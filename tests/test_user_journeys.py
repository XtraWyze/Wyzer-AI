import asyncio

from tests.fake_windows import FakeWindowsBackend
from tests.fakes import text_response, tool_response
from wyzer.app import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.tools import create_default_registry
from wyzer.workers import InProcessExecutor


def test_imperfect_spoken_application_name_is_handled_by_model_and_index() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("search_installed_applications", {"query": "crumb"})),
            tool_response(("open_application", {"application": "Google Chrome"})),
            text_response("Chrome is open."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    response = asyncio.run(assistant.handle("Open crumb"))

    assert response.text == "Chrome is open."
    assert [call.tool for call in assistant.world.snapshot().recent_tool_calls] == [
        "search_installed_applications",
        "open_application",
    ]


def test_recent_application_context_is_available_for_pronoun_followup() -> None:
    provider = FakeChatProvider([text_response("I moved it.")])
    registry = create_default_registry(FakeWindowsBackend())
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)
    assistant.conversation.mention_application("Google Chrome")

    asyncio.run(assistant.handle("Put it on the other screen"))

    system = provider.requests[0][0][0].content or ""
    assert "Google Chrome" in system
    assert "Put it on the other screen" in (provider.requests[0][0][-1].content or "")


def test_multi_tool_request_uses_one_initial_model_decision_and_ordered_results() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    provider = FakeChatProvider(
        [
            tool_response(
                ("open_application", {"application": "Calculator"}),
                ("control_master_audio", {"operation": "decrease"}),
            ),
            text_response("Calculator is open and I turned the volume down."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    response = asyncio.run(assistant.handle("Open Calculator and turn the volume down"))

    assert "Calculator" in response.text
    assert [result.tool for result in assistant.world.snapshot().recent_tool_calls] == [
        "open_application",
        "control_master_audio",
    ]
