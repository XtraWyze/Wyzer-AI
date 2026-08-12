import asyncio

from tests.fake_windows import FakeWindowsBackend
from tests.fakes import text_response, tool_response
from wyzer.app import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.tools import create_default_registry
from wyzer.workers import InProcessExecutor


def test_verified_application_launch_is_passed_to_final_model_response() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Calculator"})),
            text_response("Calculator is open."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    response = asyncio.run(assistant.handle("Open Calculator"))

    assert response.text == "Calculator is open."
    assert '"verified":true' in (provider.requests[1][0][-1].content or "")


def test_unverified_launch_result_is_available_to_model_without_fake_success() -> None:
    backend = FakeWindowsBackend()
    backend.verify_actions = False
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Calculator"})),
            text_response("Windows accepted the request, but I couldn't verify it opened."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    response = asyncio.run(assistant.handle("Open Calculator"))

    assert "couldn't verify" in response.text
    assert '"verified":false' in (provider.requests[1][0][-1].content or "")


def test_model_resolves_pronoun_minimize_to_recent_application() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Calculator"})),
            text_response("Calculator is open."),
            tool_response(
                ("control_named_window", {"window": "Calculator", "action": "minimize"})
            ),
            text_response("Calculator is minimized."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    asyncio.run(assistant.handle("Open Calculator"))
    response = asyncio.run(assistant.handle("Minimize it"))

    assert response.text == "Calculator is minimized."
    calculator = next(window for window in backend.windows if window.title == "Calculator")
    assert calculator.minimized is True
    execution_messages = provider.requests[3][0]
    resolved_call = execution_messages[-2].tool_calls[0]
    assert resolved_call.function.name == "control_named_window"
    assert resolved_call.function.arguments == {
        "window": "Calculator",
        "action": "minimize",
    }


def test_explicit_different_window_name_is_not_rewritten_to_previous_target() -> None:
    backend = FakeWindowsBackend()
    backend.launch_application("Calculator")
    next(window for window in backend.windows if window.title == "Calculator")
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Notepad"})),
            text_response("Notepad is open."),
            tool_response(("control_named_window", {"window": "Calculator", "action": "minimize"})),
            text_response("Calculator is minimized."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    asyncio.run(assistant.handle("Open Notepad"))
    asyncio.run(assistant.handle("Minimize Calculator"))

    execution_messages = provider.requests[3][0]
    current_call = execution_messages[-2].tool_calls[0]
    assert current_call.function.name == "control_named_window"
    assert current_call.function.arguments == {"window": "Calculator", "action": "minimize"}


def test_close_it_keeps_calculator_identity_and_recovers_background_close() -> None:
    from wyzer.models import ProcessInfo, WindowInfo

    class EvaluationCalculatorBackend(FakeWindowsBackend):
        def __init__(self) -> None:
            super().__init__()
            self.close_attempts = 0

        def launch_application(self, application: str) -> tuple[int | None, str]:
            assert application == "Calculator"
            process_id = 20
            self.processes.append(ProcessInfo(process_id=process_id, name="CalculatorApp.exe"))
            self.windows.append(
                WindowInfo(
                    handle=200,
                    title="Evaluation copy of Calculator",
                    process_id=process_id,
                    application="CalculatorApp.exe",
                    monitor_id="monitor:1",
                )
            )
            return process_id, "CalculatorApp.exe"

        def close_window(self, handle: int, timeout_seconds: float = 3) -> bool:
            del timeout_seconds
            self.close_attempts += 1
            if self.foreground_handle != handle:
                return False
            self.windows = [window for window in self.windows if window.handle != handle]
            return True

    backend = EvaluationCalculatorBackend()
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Calculator"})),
            text_response("Calculator is open."),
            tool_response(("control_named_window", {"window": "Calculator", "action": "close"})),
            text_response("Calculator is closed."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    opened = asyncio.run(assistant.handle("Open Calculator"))
    closed = asyncio.run(assistant.handle("Close it"))

    assert opened.text == "Calculator is open."
    assert closed.text == "Calculator is closed."
    assert backend.close_attempts == 1
    assert all(window.application != "CalculatorApp.exe" for window in backend.windows)
    repaired_call = provider.requests[3][0][-2].tool_calls[0]
    assert repaired_call.function.name == "control_named_window"
    assert repaired_call.function.arguments["window"] == "Calculator"


def test_model_resolves_pronoun_monitor_move_and_preserves_spatial_destination() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Calculator"})),
            text_response("Calculator is open."),
            tool_response(
                (
                    "move_named_window_to_monitor",
                    {
                        "window": "Calculator",
                        "destination": {"relation": "right"},
                    },
                )
            ),
            text_response("Calculator is on monitor 2."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    asyncio.run(assistant.handle("Open Calculator"))
    response = asyncio.run(assistant.handle("Move it to the monitor on the right"))

    assert response.text == "Calculator is on monitor 2."
    calculator = next(window for window in backend.windows if window.title == "Calculator")
    assert calculator.monitor_id == "monitor:2"
    execution_messages = provider.requests[3][0]
    resolved_call = execution_messages[-2].tool_calls[0]
    assert resolved_call.function.name == "move_named_window_to_monitor"
    assert resolved_call.function.arguments == {
        "window": "Calculator",
        "destination": {"relation": "right"},
    }


def test_ordered_multi_tool_window_sequence_updates_session_after_each_result() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(
                ("open_application", {"application": "Notepad"}),
                (
                    "move_named_window_to_monitor",
                    {"window": "Notepad", "destination": {"relation": "other"}},
                ),
            ),
            text_response("Notepad is open on monitor 2."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    response = asyncio.run(assistant.handle("Open Notepad and move it to the other monitor"))

    assert response.text == "Notepad is open on monitor 2."
    snapshot = assistant.session_context.snapshot()
    assert snapshot.active_window is not None
    assert snapshot.active_window.name == "Notepad"
    assert snapshot.last_monitor is not None
    assert snapshot.last_monitor.number == 2
    assert [action.tool for action in snapshot.recent_actions] == [
        "open_application",
        "move_named_window_to_monitor",
    ]
