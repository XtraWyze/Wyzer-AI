import asyncio

from tests.fake_windows import FakeWindowsBackend
from tests.fakes import text_response, tool_response
from wyzer.app import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.tasks import TaskPlanStatus, TaskStateStore
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
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Google Chrome"})),
            text_response("Chrome is open."),
            text_response("I can resolve that from the session context."),
        ]
    )
    registry = create_default_registry(FakeWindowsBackend())
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    asyncio.run(assistant.handle("Open Google Chrome"))
    asyncio.run(assistant.handle("Put it on the other screen"))

    system = provider.requests[2][0][0].content or ""
    assert '"session_context"' in system
    assert "Google Chrome" in system
    assert "Put it on the other screen" in (provider.requests[2][0][-1].content or "")


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


def test_media_skip_and_game_launch_can_finish_after_task_coordination_rounds() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    tasks = TaskStateStore()
    provider = FakeChatProvider(
        [
            tool_response(
                (
                    "task_plan_create",
                    {
                        "goal": "Skip the current song and open Rocket League",
                        "steps": [
                            {
                                "description": "Skip the current song",
                                "success_criteria": "A different current track is observed",
                            },
                            {
                                "description": "Open Rocket League",
                                "success_criteria": "The game window is visible and focused",
                            },
                        ],
                    },
                )
            ),
            tool_response(
                ("control_media", {"action": "next"}),
                ("open_application", {"application": "Rocket League"}),
            ),
            tool_response(
                ("get_current_media", {}),
                ("task_step_update", {"step_number": 1, "status": "verified"}),
            ),
            tool_response(
                ("open_application", {"application": "Rocket League"}),
                ("task_step_update", {"step_number": 2, "status": "verified"}),
            ),
            text_response("Skipped the song and opened Rocket League."),
        ]
    )
    assistant = Orchestrator(
        registry,
        InProcessExecutor(registry),
        provider,
        maximum_tool_rounds=3,
        tasks=tasks,
    )

    response = asyncio.run(assistant.handle("Can you skip this song and open Rocket League"))

    assert response.text == "Skipped the song and opened Rocket League."
    assert backend.media_actions == ["next"]
    deferred = next(
        result
        for result in assistant.world.snapshot().recent_tool_calls
        if result.error is not None
    )
    assert deferred.error is not None
    assert deferred.error.code == "TASK_STEP_VERIFICATION_REQUIRED"
    plan = tasks.snapshot()
    assert plan is not None and plan.status == TaskPlanStatus.COMPLETED
    assert [step.status.value for step in plan.steps] == ["verified", "verified"]
