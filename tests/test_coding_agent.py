from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.fakes import text_response, tool_response
from wyzer.app import Orchestrator
from wyzer.app.cli import build_assistant
from wyzer.brain import FakeChatProvider, SystemPromptBuilder
from wyzer.coding.manager import CodingAgentManager
from wyzer.coding.models import CodingAgentSettings, CodingSessionStatus
from wyzer.coding.proxy import create_coding_agent_pack
from wyzer.coding.tools import coding_native_tools
from wyzer.coding.workspace import CodingWorkspace, WorkspaceError
from wyzer.config import WyzerSettings
from wyzer.models import ChatMessage, ConversationState, WorldStateSnapshot
from wyzer.tools import SimpleToolPack, ToolRegistry, create_default_registry
from wyzer.tools.capabilities import ActivateToolCapabilityTool, ListToolCapabilitiesTool
from wyzer.workers import InProcessExecutor


def _settings(**changes: Any) -> CodingAgentSettings:
    return CodingAgentSettings(
        maximum_rounds=changes.pop("maximum_rounds", 6),
        maximum_history_messages=changes.pop("maximum_history_messages", 20),
        tool_result_context_characters=changes.pop("tool_result_context_characters", 2_000),
        command_timeout_seconds=changes.pop("command_timeout_seconds", 2),
        maximum_output_characters=changes.pop("maximum_output_characters", 1_000),
        **changes,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_pack(
        SimpleToolPack("capabilities", (ListToolCapabilitiesTool, ActivateToolCapabilityTool))
    )
    registry.register_pack(create_coding_agent_pack(), default_visible=False)
    registry.finalize_capability_activation_surface()
    return registry


def test_manager_reuses_supplied_provider_and_keeps_separate_history(tmp_path: Path) -> None:
    provider = FakeChatProvider([text_response("First complete."), text_response("Follow-up done.")])
    manager = CodingAgentManager(provider, _settings())

    first = asyncio.run(manager.start(str(tmp_path), "Initial coding task", uuid4()))
    second = asyncio.run(
        manager.message("Follow-up instruction", first["session_id"], uuid4())
    )

    assert manager.provider is provider
    assert first["session_id"] == second["session_id"]
    assert len(manager.sessions) == 1
    second_request = provider.requests[1][0]
    assert [message.content for message in second_request if message.role != "system"] == [
        "Initial coding task",
        "First complete.",
        "Follow-up instruction",
    ]
    assert "Wyzer's coding agent" in (provider.requests[0][0][0].content or "")


def test_build_assistant_gives_coding_manager_exact_main_provider() -> None:
    provider = FakeChatProvider()
    settings = WyzerSettings.model_validate(
        {
            "worker_isolation_enabled": False,
            "memory": {"enabled": False},
            "task_engine": {"enabled": False},
        }
    )

    assistant = build_assistant(settings, provider)

    assert assistant._provider is provider
    assert assistant.coding_manager is not None
    assert assistant.coding_manager.provider is provider


def test_status_is_deterministic_and_does_not_call_provider(tmp_path: Path) -> None:
    provider = FakeChatProvider([text_response("Done.")])
    manager = CodingAgentManager(provider, _settings())
    started = asyncio.run(manager.start(str(tmp_path), "Inspect", uuid4()))
    request_count = len(provider.requests)

    status = manager.status(started["session_id"])

    assert status["status"] == "idle"
    assert status["workspace"] == str(tmp_path.resolve())
    assert len(provider.requests) == request_count


def test_start_can_create_exact_explicit_new_workspace(tmp_path: Path) -> None:
    target = tmp_path / "Projects" / "TestGame"
    manager = CodingAgentManager(FakeChatProvider([text_response("Created game.")]), _settings())

    result = asyncio.run(
        manager.start(
            str(target), "Create a game", uuid4(), create_workspace=True
        )
    )

    assert target.is_dir()
    assert result["workspace"] == str(target.resolve())
    assert result["workspace_created"] is True


def test_missing_workspace_still_fails_without_explicit_creation(tmp_path: Path) -> None:
    manager = CodingAgentManager(FakeChatProvider(), _settings())

    with pytest.raises(WorkspaceError, match="not a directory"):
        asyncio.run(manager.start(str(tmp_path / "missing"), "Work", uuid4()))


def test_relative_known_user_folder_workspace_is_grounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = tmp_path / "ActualDesktop"
    desktop.mkdir()
    monkeypatch.setattr(
        "wyzer.coding.manager.common_user_folders", lambda: {"desktop": str(desktop)}
    )
    manager = CodingAgentManager(FakeChatProvider([text_response("Created.")]), _settings())

    result = asyncio.run(
        manager.start(
            r"Desktop\Projects\TestGame",
            "Create a game",
            uuid4(),
            create_workspace=True,
        )
    )

    assert result["workspace"] == str((desktop / "Projects" / "TestGame").resolve())
    assert result["workspace_created"] is True


def test_unanchored_relative_workspace_is_rejected() -> None:
    manager = CodingAgentManager(FakeChatProvider(), _settings())

    with pytest.raises(RuntimeError, match="absolute workspace"):
        asyncio.run(manager.start("some/relative/project", "Work", uuid4()))


def test_workspace_rejects_traversal_and_absolute_outside_paths(tmp_path: Path) -> None:
    workspace = CodingWorkspace(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="inside the assigned workspace"):
        workspace.read_file("../outside.txt")
    with pytest.raises(WorkspaceError, match="inside the assigned workspace"):
        workspace.read_file(str(outside.resolve()))
    with pytest.raises(WorkspaceError, match="inside the assigned workspace"):
        workspace.resolve("missing/../../outside.txt")


def test_workspace_reads_and_exactly_edits_inspected_file(tmp_path: Path) -> None:
    target = tmp_path / "example.py"
    target.write_text("answer = 41\n", encoding="utf-8")
    workspace = CodingWorkspace(tmp_path)

    read = workspace.read_file("example.py")
    edited = workspace.edit_file(
        "example.py", "answer = 41", "answer = 42", expected_sha256=read["sha256"]
    )

    assert read["content"].replace("\r\n", "\n") == "answer = 41\n"
    assert edited["changed"] is True
    assert edited["occurrences_changed"] == 1
    assert target.read_text(encoding="utf-8").replace("\r\n", "\n") == "answer = 42\n"
    assert workspace.changed_files == {"example.py"}


def test_existing_file_must_be_inspected_and_hash_mismatch_fails(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_text("one", encoding="utf-8")
    workspace = CodingWorkspace(tmp_path)

    with pytest.raises(WorkspaceError, match="Read the file"):
        workspace.edit_file("example.txt", "one", "two")
    workspace.read_file("example.txt")
    with pytest.raises(WorkspaceError, match="changed since"):
        workspace.edit_file(
            "example.txt", "one", "two", expected_sha256="0" * 64
        )


def test_command_cwd_cannot_leave_workspace(tmp_path: Path) -> None:
    workspace = CodingWorkspace(tmp_path)

    with pytest.raises(WorkspaceError, match="inside the assigned workspace"):
        asyncio.run(workspace.run_command(["py", "--version"], cwd=".."))


def test_command_output_is_bounded_and_exit_code_is_structured(tmp_path: Path) -> None:
    workspace = CodingWorkspace(tmp_path, maximum_output_characters=1_000)

    result = asyncio.run(
        workspace.run_command(["py", "-c", "print('x' * 5000)"])
    )

    assert result["exit_code"] == 0
    assert len(result["stdout"]) == 1_000
    assert result["output_truncated"] is True
    assert result["timed_out"] is False


def test_command_timeout_is_reported(tmp_path: Path) -> None:
    workspace = CodingWorkspace(tmp_path, command_timeout_seconds=0.05)

    result = asyncio.run(
        workspace.run_command(
            ["py", "-c", "import time; time.sleep(5)"], timeout_seconds=0.05
        )
    )

    assert result["timed_out"] is True
    assert result["exit_code"] != 0


def test_command_accepts_bounded_stdin_for_interactive_smoke_test(tmp_path: Path) -> None:
    workspace = CodingWorkspace(tmp_path)

    result = asyncio.run(
        workspace.run_command(
            ["py", "-c", "print(input('Value: '))"], stdin="42\n"
        )
    )

    assert result["exit_code"] == 0
    assert "42" in result["stdout"]
    assert result["stdin_provided"] is True


def test_agent_loop_has_hard_round_limit(tmp_path: Path) -> None:
    provider = FakeChatProvider(
        [
            tool_response(("code_list_directory", {})),
            tool_response(("code_list_directory", {})),
        ]
    )
    manager = CodingAgentManager(provider, _settings(maximum_rounds=2))

    result = asyncio.run(manager.start(str(tmp_path), "Loop", uuid4()))

    assert result["status"] == "failed"
    assert "2 rounds" in result["last_summary"]
    assert len(provider.requests) == 2


def test_verification_comes_from_observed_change_and_successful_check(tmp_path: Path) -> None:
    target = tmp_path / "example.py"
    target.write_text("answer = 41\n", encoding="utf-8")
    provider = FakeChatProvider(
        [
            tool_response(("code_read_file", {"path": "example.py"})),
            tool_response(
                (
                    "code_edit_file",
                    {
                        "path": "example.py",
                        "old_text": "answer = 41",
                        "new_text": "answer = 42",
                    },
                )
            ),
            tool_response(
                ("code_run_command", {"argv": ["py", "-m", "py_compile", "example.py"]})
            ),
            text_response("Changed the answer and compiled the file."),
        ]
    )
    manager = CodingAgentManager(provider, _settings())

    result = asyncio.run(manager.start(str(tmp_path), "Update the answer", uuid4()))

    assert result["changed_files"] == ["example.py"]
    assert result["last_verification"]["verification_status"] == "verified"
    assert result["last_verification"]["successful_checks"][0]["exit_code"] == 0


def test_model_prose_alone_does_not_create_verification(tmp_path: Path) -> None:
    manager = CodingAgentManager(
        FakeChatProvider([text_response("Everything passed, allegedly.")]), _settings()
    )

    result = asyncio.run(manager.start(str(tmp_path), "Claim success", uuid4()))

    assert result["last_verification"]["verification_status"] == "unavailable"


class _BlockingProvider:
    available = True

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.requests: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage], tools: list[Any], settings: Any = None) -> Any:
        del tools, settings
        self.requests.append(messages)
        self.started.set()
        await asyncio.Future()


def test_manager_cancellation_stops_active_provider_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _BlockingProvider()
        manager = CodingAgentManager(provider, _settings())
        operation = asyncio.create_task(manager.start(str(tmp_path), "Wait", uuid4()))
        await provider.started.wait()
        session_id = str(manager.sessions[0].session_id)

        cancelled = manager.cancel(session_id)
        result = await asyncio.wait_for(operation, timeout=2)

        assert cancelled["cancelled"] is True
        assert result["status"] == "cancelled"

    asyncio.run(scenario())


def test_manager_cancellation_stops_active_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeChatProvider(
            [
                tool_response(
                    (
                        "code_run_command",
                        {"argv": ["py", "-c", "import time; time.sleep(30)"]},
                    )
                )
            ]
        )
        manager = CodingAgentManager(provider, _settings(command_timeout_seconds=40))
        operation = asyncio.create_task(manager.start(str(tmp_path), "Run then wait", uuid4()))
        for _ in range(200):
            if manager.sessions and manager._workspaces[manager.sessions[0].session_id]._process:
                break
            await asyncio.sleep(0.01)
        session_id = str(manager.sessions[0].session_id)

        cancelled = manager.cancel(session_id)
        result = await asyncio.wait_for(operation, timeout=3)

        assert cancelled["cancelled"] is True
        assert result["status"] == "cancelled"
        assert result["commands_run"][0]["exit_code"] != 0

    asyncio.run(scenario())


def test_coding_tool_schemas_stay_small_and_focused() -> None:
    tools = coding_native_tools()

    assert {tool.function.name for tool in tools} == {
        "code_list_directory",
        "code_read_file",
        "code_search",
        "code_write_file",
        "code_edit_file",
        "code_run_command",
        "code_git_status",
        "code_git_diff",
    }
    assert len(tools) == 8
    assert len(str([tool.model_dump() for tool in tools])) < 8_000


def test_coding_pack_can_be_registered_as_an_optional_capability() -> None:
    registry = _registry()

    assert "coding_agent_start" not in registry.model_view().tool_names
    assert "activate_coding_agent_tools" in registry.model_view().tool_names
    assert "coding_agent_start" in registry.model_view(("coding_agent",)).tool_names


def test_default_registry_exposes_only_four_direct_coding_proxies() -> None:
    names = set(create_default_registry().model_view().tool_names)

    assert {
        "coding_agent_start",
        "coding_agent_message",
        "coding_agent_status",
        "coding_agent_cancel",
    } <= names
    assert "activate_coding_agent_tools" not in names


def test_main_prompt_routes_development_and_followups_to_coding_agent() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "Software-development work" in prompt
    assert "not generic file tools" in prompt
    assert "'try again' always means continue, not cancel" in prompt
    assert "coding_agent_cancel only when" in prompt


class _SpyExecutor(InProcessExecutor):
    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)
        self.names: list[str] = []

    async def execute(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        self.names.append(tool_name)
        return await super().execute(tool_name, *args, **kwargs)


def test_orchestrator_intercepts_proxy_and_records_normal_result(tmp_path: Path) -> None:
    registry = _registry()
    provider = FakeChatProvider(
        [
            tool_response(("activate_coding_agent_tools", {})),
            tool_response(
                ("coding_agent_start", {"workspace": str(tmp_path), "task": "Inspect project"})
            ),
            text_response("Coding inspection complete."),
            text_response("The coding agent finished."),
        ]
    )
    manager = CodingAgentManager(provider, _settings())
    executor = _SpyExecutor(registry)
    assistant = Orchestrator(registry, executor, provider, coding_manager=manager)

    response = asyncio.run(assistant.handle("Ask the coding agent to inspect this project"))

    assert response.text == "The coding agent finished."
    assert "coding_agent_start" not in executor.names
    result = assistant.conversation.snapshot().recent_tool_results[-1]
    assert result.tool == "coding_agent_start"
    assert result.ok is True
    assert result.data is not None
    assert result.data["last_summary"] == "Coding inspection complete."
    assert result.evidence["predicate"] == "coding_task_completed_and_checked"
    assert manager.provider is assistant._provider


def test_main_history_does_not_receive_coding_tool_transcript(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("CODING_ONLY_RAW_CONTENT", encoding="utf-8")
    registry = _registry()
    provider = FakeChatProvider(
        [
            tool_response(("activate_coding_agent_tools", {})),
            tool_response(
                ("coding_agent_start", {"workspace": str(tmp_path), "task": "Read the file"})
            ),
            tool_response(("code_read_file", {"path": "secret.txt"})),
            text_response("Inspected the requested file."),
            text_response("Inspection finished."),
        ]
    )
    manager = CodingAgentManager(provider, _settings())
    assistant = Orchestrator(
        registry, InProcessExecutor(registry), provider, coding_manager=manager
    )

    asyncio.run(assistant.handle("Delegate the file inspection"))

    main_final_request = provider.requests[-1][0]
    assert not any("CODING_ONLY_RAW_CONTENT" in (message.content or "") for message in main_final_request)
    coding_request = provider.requests[3][0]
    assert any("CODING_ONLY_RAW_CONTENT" in (message.content or "") for message in coding_request)
    assert any("CODING_AGENT_CONTEXT_JSON=" in (message.content or "") for message in main_final_request)


def test_orchestrator_interrupt_cancels_active_coding_operation(tmp_path: Path) -> None:
    class SequencedProvider:
        available = True

        def __init__(self) -> None:
            self.calls = 0
            self.coding_started = asyncio.Event()

        async def chat(self, messages: list[ChatMessage], tools: list[Any], settings: Any = None) -> Any:
            del messages, tools, settings
            self.calls += 1
            if self.calls == 1:
                return tool_response(("activate_coding_agent_tools", {}))
            if self.calls == 2:
                return tool_response(
                    ("coding_agent_start", {"workspace": str(tmp_path), "task": "Wait"})
                )
            self.coding_started.set()
            await asyncio.Future()

    async def scenario() -> None:
        registry = _registry()
        provider = SequencedProvider()
        manager = CodingAgentManager(provider, _settings())
        assistant = Orchestrator(
            registry, InProcessExecutor(registry), provider, coding_manager=manager
        )
        operation = asyncio.create_task(assistant.handle("Delegate work"))
        await provider.coding_started.wait()

        assert assistant.interrupt() is True
        response = await asyncio.wait_for(operation, timeout=2)

        assert response.interrupted is True
        assert manager.sessions[0].status == CodingSessionStatus.CANCELLED

    asyncio.run(scenario())
