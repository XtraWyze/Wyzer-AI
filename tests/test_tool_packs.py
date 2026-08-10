from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest

from tests.fake_windows import FakeWindowsBackend
from tests.fakes import EchoArguments, EchoData, EchoTool
from wyzer.models import RiskLevel
from wyzer.tools import (
    CallableTool,
    SimpleToolPack,
    ToolContext,
    ToolRegistry,
    create_default_registry,
)
from wyzer.tools.discovery import ToolPackDiscoveryError, load_enabled_tool_packs
from wyzer.tools.registry import DuplicateToolError, DuplicateToolPackError
from wyzer.workers import InProcessExecutor


def _echo_handler(arguments: EchoArguments, context: ToolContext) -> EchoData:
    del context
    return EchoData(echoed=arguments.message)


def _callable_echo_tool() -> CallableTool[EchoArguments, EchoData]:
    return CallableTool(
        name="callable_echo",
        description="Echo text through a normal Python function.",
        arguments_type=EchoArguments,
        result_type=EchoData,
        handler=_echo_handler,
        risk_level=RiskLevel.LOW,
        read_only=True,
    )


def test_registry_registers_named_pack_and_tracks_ownership() -> None:
    registry = ToolRegistry()
    registry.register_pack(SimpleToolPack("example", (EchoTool,)))

    assert registry.pack_names() == ("example",)
    assert registry.pack_tools("example") == ("echo",)
    assert registry.tool_pack("echo") == "example"
    assert registry.compact_manifest()[0]["pack"] == "example"
    assert registry.pack_manifest() == [{"name": "example", "tools": ["echo"], "count": 1}]


def test_pack_registration_is_atomic_when_one_tool_conflicts() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    pack = SimpleToolPack("conflicting", (EchoTool, _callable_echo_tool))

    with pytest.raises(DuplicateToolError):
        registry.register_pack(pack)

    assert "callable_echo" not in registry
    assert registry.pack_names() == ()


def test_registry_rejects_duplicate_pack_name() -> None:
    registry = ToolRegistry()
    registry.register_pack(SimpleToolPack("example", (EchoTool,)))

    with pytest.raises(DuplicateToolPackError):
        registry.register_pack(SimpleToolPack("example", (_callable_echo_tool,)))


def test_callable_tool_executes_through_normal_executor() -> None:
    registry = ToolRegistry()
    registry.register_pack(SimpleToolPack("functions", (_callable_echo_tool,)))

    result = asyncio.run(
        InProcessExecutor(registry).execute(
            "callable_echo",
            {"message": "hello"},
            uuid4(),
            uuid4(),
        )
    )

    assert result.ok is True
    assert result.data == {"echoed": "hello"}


@dataclass(frozen=True)
class _FakeEntryPoint:
    name: str
    value: object

    def load(self) -> object:
        return self.value


class _FakeEntryPoints(tuple[_FakeEntryPoint, ...]):
    def select(self, *, group: str) -> _FakeEntryPoints:
        assert group == "wyzer.tool_packs"
        return self


def _example_pack() -> SimpleToolPack:
    return SimpleToolPack("example", (EchoTool,))


def test_default_factory_composes_builtin_and_extra_packs() -> None:
    registry = create_default_registry(
        FakeWindowsBackend(),
        extra_pack_factories=(_example_pack,),
    )

    assert registry.pack_names() == (
        "applications",
        "audio",
        "browser",
        "capabilities",
        "clipboard",
        "desktop_interaction",
        "diagnostics",
        "example",
        "files",
        "media",
        "perception",
        "system",
        "windows",
    )
    assert registry.tool_pack("echo") == "example"
    assert registry.tool_pack("open_application") == "applications"
    assert registry.tool_pack("search_files") == "files"


def test_default_model_view_keeps_basic_actions_and_scopes_specialized_packs() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    default_names = set(registry.model_view().tool_names)
    all_names = {tool.function.name for tool in registry.all_native_tools()}

    assert {
        "open_application",
        "control_application_audio",
        "control_named_window",
        "move_named_window_to_monitor",
        "list_tool_capabilities",
        "activate_tool_capability",
    } <= default_names
    assert {"browser_search_web", "search_files", "inspect_screen"}.isdisjoint(default_names)
    assert {"browser_search_web", "search_files", "inspect_screen"} <= all_names


def test_entrypoint_packs_load_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wyzer.tools import discovery

    monkeypatch.setattr(
        discovery,
        "entry_points",
        lambda: _FakeEntryPoints((_FakeEntryPoint("example", _example_pack),)),
    )

    assert load_enabled_tool_packs(()) == ()
    packs = load_enabled_tool_packs(("example",))
    assert len(packs) == 1
    assert packs[0].name == "example"


def test_missing_enabled_entrypoint_pack_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wyzer.tools import discovery

    monkeypatch.setattr(discovery, "entry_points", lambda: _FakeEntryPoints(()))

    with pytest.raises(ToolPackDiscoveryError, match="not installed"):
        load_enabled_tool_packs(("missing",))
