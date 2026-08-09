"""Tool contracts, packs, and the authoritative registry."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from wyzer.tools.base import Tool, ToolContext, ToolExecutionError
from wyzer.tools.registry import ToolRegistry

TOOL_PACK_ENTRYPOINT_GROUP = "wyzer.tool_packs"

if TYPE_CHECKING:
    from wyzer.desktop.system import WindowsSystemBackend
    from wyzer.tools.packs import (
        CallableTool,
        SimpleToolPack,
        ToolFactory,
        ToolPack,
        ToolPackFactory,
    )


def __getattr__(name: str) -> Any:
    """Lazily expose pack helpers without slowing isolated worker startup."""
    pack_exports = {
        "CallableTool",
        "SimpleToolPack",
        "ToolFactory",
        "ToolPack",
        "ToolPackFactory",
    }
    if name in pack_exports:
        from wyzer.tools import packs

        return getattr(packs, name)
    raise AttributeError(name)


def discover_tool_pack_names() -> tuple[str, ...]:
    """List installed external packs without slowing normal worker imports."""
    from wyzer.tools.discovery import discover_tool_pack_names as discover

    return discover()


def create_default_registry(
    backend: WindowsSystemBackend | None = None,
    *,
    audio_options: dict[str, object] | None = None,
    perception_options: dict[str, object] | None = None,
    enabled_entrypoint_packs: Sequence[str] = (),
    extra_pack_factories: Sequence[ToolPackFactory] = (),
) -> ToolRegistry:
    from wyzer.tools.factory import create_default_registry as create

    return create(
        backend,
        audio_options=audio_options,
        perception_options=perception_options,
        enabled_entrypoint_packs=enabled_entrypoint_packs,
        extra_pack_factories=extra_pack_factories,
    )


__all__ = [
    "TOOL_PACK_ENTRYPOINT_GROUP",
    "CallableTool",
    "SimpleToolPack",
    "Tool",
    "ToolContext",
    "ToolExecutionError",
    "ToolFactory",
    "ToolPack",
    "ToolPackFactory",
    "ToolRegistry",
    "create_default_registry",
    "discover_tool_pack_names",
]
