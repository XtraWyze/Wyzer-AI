import asyncio
import builtins
from uuid import uuid4

from pytest import CaptureFixture, MonkeyPatch

from tests.fakes import (
    EchoTool,
    FailingTool,
    OpenApplicationTool,
    SlowEchoTool,
    VerifiedActionTool,
    text_response,
    tool_response,
)
from wyzer.app import Orchestrator
from wyzer.app.cli import chat
from wyzer.brain import FakeChatProvider
from wyzer.models import ChatMessage, ProviderChatResponse
from wyzer.tasks import TaskPlanStatus, TaskStateStore
from wyzer.tools import ToolRegistry
from wyzer.workers import InProcessExecutor


def build_assistant(
    registry: ToolRegistry,
    provider: FakeChatProvider,
    *,
    maximum_tool_rounds: int = 6,
    tasks: TaskStateStore | None = None,
) -> Orchestrator:
    return Orchestrator(
        registry,
        InProcessExecutor(registry),
        provider,
        maximum_tool_rounds=maximum_tool_rounds,
        tasks=tasks,
    )


def test_normal_conversation_uses_exactly_one_provider_request() -> None:
    provider = FakeChatProvider([text_response("I'm doing well.")])
    assistant = build_assistant(ToolRegistry(), provider)

    response = asyncio.run(assistant.handle("How are you?"))

    assert response.text == "I'm doing well."
    assert len(provider.requests) == 1
    assert assistant.world.snapshot().recent_tool_calls == []


def test_detailed_request_allows_a_larger_response() -> None:
    provider = FakeChatProvider([text_response("A detailed answer.")])
    assistant = build_assistant(ToolRegistry(), provider)

    asyncio.run(assistant.handle("Explain this in detail"))

    assert provider.request_settings[0] is not None
    assert provider.request_settings[0].max_output_tokens == 1024


def test_computer_request_keeps_full_prompt_and_tool_schemas() -> None:
    registry = ToolRegistry()
    registry.register(OpenApplicationTool())
    provider = FakeChatProvider(
        [tool_response(("open_application", {"application": "Chrome"})), text_response("Done.")]
    )
    assistant = build_assistant(registry, provider)

    asyncio.run(assistant.handle("Open Chrome"))

    messages, tools = provider.requests[0]
    assert any(tool.function.name == "open_application" for tool in tools)
    assert "CONTEXT_JSON=" in (messages[0].content or "")


def test_help_is_local_and_lists_commands() -> None:
    provider = FakeChatProvider([text_response("unused")])
    assistant = build_assistant(ToolRegistry(), provider)

    response = asyncio.run(assistant.handle("help"))

    assert "open and control applications" in response.text
    assert "'stop' or 'cancel'" in response.text
    assert provider.requests == []


def test_simple_action_executes_without_confirmation_and_returns_tool_message() -> None:
    registry = ToolRegistry()
    registry.register(OpenApplicationTool())
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Chrome"})),
            text_response("Chrome is open."),
        ]
    )
    assistant = build_assistant(registry, provider)

    response = asyncio.run(assistant.handle("Open Chrome"))

    assert response.text == "Chrome is open."
    assert len(provider.requests) == 2
    second_messages = provider.requests[1][0]
    assert second_messages[-2].role == "assistant"
    assert second_messages[-2].tool_calls[0].function.name == "open_application"
    assert second_messages[-1].role == "tool"
    assert second_messages[-1].name == "open_application"
    assert '"ok":true' in (second_messages[-1].content or "")
    assert assistant.world.snapshot().pending_confirmation is None


def test_multiple_tool_calls_execute_in_returned_order() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    provider = FakeChatProvider(
        [
            tool_response(("echo", {"message": "first"}), ("echo", {"message": "second"})),
            text_response("Both are done."),
        ]
    )
    assistant = build_assistant(registry, provider)

    response = asyncio.run(assistant.handle("Echo both"))

    assert response.text == "Both are done."
    results = assistant.world.snapshot().recent_tool_calls
    assert [result.data for result in results] == [
        {"echoed": "first"},
        {"echoed": "second"},
    ]
    assert [message.role for message in provider.requests[1][0][-3:]] == [
        "assistant",
        "tool",
        "tool",
    ]


def test_unknown_tool_is_structured_error_and_never_executes() -> None:
    provider = FakeChatProvider(
        [
            tool_response(("run_powershell", {"command": "whoami"})),
            text_response("I can't do that."),
        ]
    )
    assistant = build_assistant(ToolRegistry(), provider)

    response = asyncio.run(assistant.handle("Run a command"))

    assert response.text == "I can't do that."
    result = assistant.world.snapshot().recent_tool_calls[-1]
    assert result.error is not None and result.error.code == "UNKNOWN_TOOL"
    assert '"code":"UNKNOWN_TOOL"' in (provider.requests[1][0][-1].content or "")


def test_invalid_arguments_are_returned_and_model_can_correct_them() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    provider = FakeChatProvider(
        [
            tool_response(("echo", {"wrong": "value"})),
            tool_response(("echo", {"message": "fixed"})),
            text_response("Fixed it."),
        ]
    )
    assistant = build_assistant(registry, provider)

    response = asyncio.run(assistant.handle("Echo this"))

    assert response.text == "Fixed it."
    results = assistant.world.snapshot().recent_tool_calls
    assert results[0].error is not None
    assert results[0].error.code == "INVALID_TOOL_ARGUMENTS"
    assert results[1].ok is True


def test_tool_failure_is_returned_to_model_for_grounded_final_text() -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())
    provider = FakeChatProvider(
        [
            tool_response(("failing", {"message": "x"})),
            text_response("It failed: expected failure."),
        ]
    )
    assistant = build_assistant(registry, provider)

    response = asyncio.run(assistant.handle("Try it"))

    assert "failed" in response.text
    tool_message = provider.requests[1][0][-1]
    assert '"ok":false' in (tool_message.content or "")
    assert "expected failure" in (tool_message.content or "")


def test_empty_response_retries_only_once() -> None:
    provider = FakeChatProvider(
        [
            ProviderChatResponse(message=ChatMessage(role="assistant", content="")),
            text_response("Recovered."),
        ]
    )
    assistant = build_assistant(ToolRegistry(), provider)

    response = asyncio.run(assistant.handle("Hello"))

    assert response.text == "Recovered."
    assert len(provider.requests) == 2
    assert "valid native tool call" in (provider.requests[1][0][-1].content or "")


def test_hard_tool_round_limit_stops_loop_honestly() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    provider = FakeChatProvider(
        [
            tool_response(("echo", {"message": "one"})),
            tool_response(("echo", {"message": "two"})),
        ]
    )
    assistant = build_assistant(registry, provider, maximum_tool_rounds=1)

    response = asyncio.run(assistant.handle("Loop"))

    assert "stopped after 1 tool rounds" in response.text
    results = assistant.world.snapshot().recent_tool_calls
    assert len(results) == 1
    assert all(result.data != {"echoed": "second"} for result in results)


def test_interruption_prevents_remaining_calls() -> None:
    async def scenario() -> tuple[Orchestrator, str]:
        registry = ToolRegistry()
        registry.register(SlowEchoTool())
        provider = FakeChatProvider(
            [
                tool_response(
                    ("slow_echo", {"message": "first"}),
                    ("slow_echo", {"message": "second"}),
                )
            ]
        )
        assistant = build_assistant(registry, provider)
        task = asyncio.create_task(assistant.handle("Do both"))
        await asyncio.sleep(0.01)
        assert assistant.interrupt() is True
        response = await task
        return assistant, response.text

    assistant, text = asyncio.run(scenario())
    assert "stopped" in text
    results = assistant.world.snapshot().recent_tool_calls
    assert len(results) <= 1
    assert all(result.data != {"echoed": "second"} for result in results)


def test_stop_is_local_and_does_not_call_provider() -> None:
    provider = FakeChatProvider([text_response("unused")])
    response = asyncio.run(build_assistant(ToolRegistry(), provider).handle("stop"))
    assert response.text == "There is no active task."
    assert provider.requests == []


def test_llm_can_silently_plan_and_verify_multistep_work() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    tasks = TaskStateStore()
    provider = FakeChatProvider(
        [
            tool_response(
                ("echo", {"message": "first"}),
                (
                    "task_plan_create",
                    {
                        "goal": "Echo two values",
                        "steps": [
                            {
                                "description": "Echo the first value",
                                "success_criteria": "The first value is observed",
                            },
                            {
                                "description": "Echo the second value",
                                "success_criteria": "The second value is observed",
                            },
                        ],
                    },
                ),
            ),
            tool_response(
                ("task_step_update", {"step_number": 1, "status": "verified"}),
            ),
            tool_response(
                ("echo", {"message": "second"}),
                ("task_step_update", {"step_number": 2, "status": "verified"}),
            ),
            text_response("Both values were handled."),
        ]
    )
    assistant = build_assistant(registry, provider, tasks=tasks)

    response = asyncio.run(assistant.handle("Echo first and then second"))

    assert response.text == "Both values were handled."
    plan = tasks.snapshot()
    assert plan is not None and plan.status == TaskPlanStatus.COMPLETED
    assert all(step.status.value == "verified" for step in plan.steps)
    assert [result.tool for result in assistant.world.snapshot().recent_tool_calls[:2]] == [
        "task_plan_create",
        "echo",
    ]
    exposed = {tool.function.name for tool in provider.requests[0][1]}
    assert {"task_plan_create", "task_step_update", "task_plan_revise"} <= exposed


def test_unfinished_plan_prevents_unsupported_completion_claim() -> None:
    tasks = TaskStateStore()
    provider = FakeChatProvider(
        [
            tool_response(
                (
                    "task_plan_create",
                    {
                        "goal": "Do two things",
                        "steps": [
                            {"description": "First", "success_criteria": "First verified"},
                            {"description": "Second", "success_criteria": "Second verified"},
                        ],
                    },
                )
            ),
            text_response("Everything is done."),
            tool_response(
                (
                    "task_step_update",
                    {"step_number": 1, "status": "blocked", "note": "No tool available"},
                )
            ),
            text_response("I couldn't perform the first step."),
        ]
    )
    assistant = build_assistant(ToolRegistry(), provider, tasks=tasks)

    response = asyncio.run(assistant.handle("Do two things"))

    assert response.text == "I couldn't perform the first step."
    assert len(provider.requests) == 4
    assert "previous response tried to finish" in (provider.requests[2][0][-1].content or "")
    plan = tasks.snapshot()
    assert plan is not None and plan.status == TaskPlanStatus.BLOCKED


def test_multiple_capability_calls_require_llm_authored_plan() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    tasks = TaskStateStore()
    provider = FakeChatProvider(
        [
            tool_response(
                ("echo", {"message": "first"}),
                ("echo", {"message": "second"}),
            ),
            tool_response(
                (
                    "task_plan_create",
                    {
                        "goal": "Echo twice",
                        "steps": [
                            {"description": "First echo", "success_criteria": "First observed"},
                            {
                                "description": "Second echo",
                                "success_criteria": "Second observed",
                            },
                        ],
                    },
                )
            ),
            tool_response(
                ("echo", {"message": "first"}),
                ("task_step_update", {"step_number": 1, "status": "verified"}),
            ),
            tool_response(
                ("echo", {"message": "second"}),
                ("task_step_update", {"step_number": 2, "status": "verified"}),
            ),
            text_response("Done."),
        ]
    )
    assistant = build_assistant(registry, provider, tasks=tasks)

    response = asyncio.run(assistant.handle("Echo two values"))

    assert response.text == "Done."
    results = assistant.world.snapshot().recent_tool_calls
    assert results[0].error is not None
    assert results[0].error.code == "TASK_PLAN_REQUIRED"
    assert not any(result.data == {"echoed": "first"} for result in results[:2])
    assert tasks.snapshot().status == TaskPlanStatus.COMPLETED  # type: ignore[union-attr]


def test_capability_calls_after_plan_completion_never_execute() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    tasks = TaskStateStore()
    provider = FakeChatProvider(
        [
            tool_response(
                (
                    "task_plan_create",
                    {
                        "goal": "Echo safely",
                        "steps": [
                            {"description": "First", "success_criteria": "First observed"},
                            {"description": "Second", "success_criteria": "Second observed"},
                        ],
                    },
                )
            ),
            tool_response(
                ("echo", {"message": "first"}),
                ("task_step_update", {"step_number": 1, "status": "verified"}),
            ),
            tool_response(
                ("echo", {"message": "second"}),
                ("task_step_update", {"step_number": 2, "status": "verified"}),
                ("echo", {"message": "must-not-run"}),
            ),
            text_response("Finished safely."),
        ]
    )
    assistant = build_assistant(registry, provider, tasks=tasks)

    response = asyncio.run(assistant.handle("Echo safely twice"))

    assert response.text == "Finished safely."
    results = assistant.world.snapshot().recent_tool_calls
    rejected = next(result for result in results if result.tool == "echo" and not result.ok)
    assert rejected.error is not None
    assert rejected.error.code == "TASK_ALREADY_COMPLETED"
    assert all(result.data != {"echoed": "must-not-run"} for result in results)


def test_verified_step_cannot_absorb_later_actions_or_loop_after_completion() -> None:
    registry = ToolRegistry()
    registry.register(VerifiedActionTool())
    tasks = TaskStateStore()
    provider = FakeChatProvider(
        [
            tool_response(
                (
                    "task_plan_create",
                    {
                        "goal": "Perform both actions",
                        "steps": [
                            {"description": "First", "success_criteria": "First verified"},
                            {"description": "Second", "success_criteria": "Second verified"},
                        ],
                    },
                )
            ),
            tool_response(
                ("verified_action", {"message": "first"}),
                ("verified_action", {"message": "second-too-early"}),
            ),
            tool_response(
                ("task_step_update", {"step_number": 1, "status": "verified"}),
                ("verified_action", {"message": "second"}),
            ),
            tool_response(
                ("task_step_update", {"step_number": 2, "status": "verified"}),
            ),
            # A misbehaving local model may still emit a call even with no tools offered.
            tool_response(("verified_action", {"message": "must-not-run"})),
        ]
    )
    assistant = build_assistant(registry, provider, tasks=tasks)

    response = asyncio.run(assistant.handle("Perform both actions"))

    assert response.text == "Done — Perform both actions."
    plan = tasks.snapshot()
    assert plan is not None and plan.status == TaskPlanStatus.COMPLETED
    assert [len(step.evidence) for step in plan.steps] == [1, 1]
    results = assistant.world.snapshot().recent_tool_calls
    deferred = next(result for result in results if result.error is not None)
    assert deferred.error is not None
    assert deferred.error.code == "TASK_STEP_UPDATE_REQUIRED"
    assert all(result.data != {"message": "must-not-run"} for result in results)
    assert provider.requests[-1][1] == []


def test_task_status_and_pause_are_small_local_controls() -> None:
    tasks = TaskStateStore()
    tasks.create(
        uuid4(),
        "Organize files",
        [
            {"description": "Find files", "success_criteria": "Files listed"},
            {"description": "Move files", "success_criteria": "Moves verified"},
        ],
    )
    provider = FakeChatProvider([text_response("unused")])
    assistant = build_assistant(ToolRegistry(), provider, tasks=tasks)

    status = asyncio.run(assistant.handle("task status"))
    paused = asyncio.run(assistant.handle("pause"))

    assert "Organize files (active)" in status.text
    assert "paused the task" in paused.text
    assert paused.interrupted is True
    assert tasks.snapshot().status == TaskPlanStatus.PAUSED  # type: ignore[union-attr]
    assert provider.requests == []


def test_paused_task_does_not_leak_into_unrelated_conversation() -> None:
    tasks = TaskStateStore()
    tasks.create(
        uuid4(),
        "Minimize old windows",
        [
            {"description": "Minimize Wyzer", "success_criteria": "Wyzer is minimized"},
            {"description": "Minimize Program Manager", "success_criteria": "Desktop hidden"},
        ],
    )
    tasks.pause()
    provider = FakeChatProvider([text_response("Not much. What's up with you?")])
    assistant = build_assistant(ToolRegistry(), provider, tasks=tasks)

    response = asyncio.run(assistant.handle("What's up?"))

    assert response.text == "Not much. What's up with you?"
    messages = provider.requests[0][0]
    assert all("TASK_PLAN_JSON=" not in (message.content or "") for message in messages)


def test_explicit_resume_restores_paused_task_context() -> None:
    tasks = TaskStateStore()
    tasks.create(
        uuid4(),
        "Continue organizing files",
        [
            {"description": "Find files", "success_criteria": "Files listed"},
            {"description": "Move files", "success_criteria": "Moves verified"},
        ],
    )
    tasks.pause()
    provider = FakeChatProvider([text_response("I'll continue the saved task.")])
    assistant = build_assistant(ToolRegistry(), provider, tasks=tasks)

    asyncio.run(assistant.handle("resume"))

    messages = provider.requests[0][0]
    task_messages = [
        message for message in messages if "TASK_PLAN_JSON=" in (message.content or "")
    ]
    assert len(task_messages) == 1
    assert "Continue organizing files" in (task_messages[0].content or "")


def test_terminal_chat_accepts_stop_while_action_is_running(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    registry = ToolRegistry()
    registry.register(SlowEchoTool())
    provider = FakeChatProvider(
        [
            tool_response(
                ("slow_echo", {"message": "first"}),
                ("slow_echo", {"message": "second"}),
            )
        ]
    )
    assistant = build_assistant(registry, provider)
    lines = iter(["do both", "stop", "quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(lines))

    asyncio.run(chat(assistant, "Wyzer"))

    assert "Wyzer: Okay, I stopped it." in capsys.readouterr().out
    assert all(
        result.data != {"echoed": "second"}
        for result in assistant.world.snapshot().recent_tool_calls
    )
