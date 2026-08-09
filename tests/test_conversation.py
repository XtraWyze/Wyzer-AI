from datetime import UTC, datetime
from uuid import uuid4

from wyzer.conversation import ConversationManager
from wyzer.models import AssistantResponse, ToolResult, UserRequest


def test_conversation_history_is_bounded() -> None:
    manager = ConversationManager(limit=2)
    for text in ["one", "two", "three"]:
        manager.record_user(UserRequest(text=text))
    assert manager.snapshot().recent_user_messages == ["two", "three"]


def test_conversation_snapshot_is_detached() -> None:
    manager = ConversationManager()
    manager.record_response(
        AssistantResponse(text="hello", action_id=UserRequest(text="x").request_id)
    )
    snapshot = manager.snapshot()
    snapshot.recent_assistant_responses.append("changed")
    assert manager.snapshot().recent_assistant_responses == ["hello"]


def test_conversation_keeps_chronological_ephemeral_transcript() -> None:
    manager = ConversationManager(limit=4)
    request = UserRequest(text="open the game")
    manager.record_user(request)
    manager.record_response(AssistantResponse(text="Trying it.", action_id=request.request_id))
    manager.record_user(UserRequest(text="it worked"))

    transcript = manager.snapshot().recent_transcript
    assert [entry["role"] for entry in transcript] == ["user", "assistant", "user"]
    assert transcript[-1]["content"] == "it worked"


def test_live_application_check_becomes_the_recent_pronoun_target() -> None:
    manager = ConversationManager()
    now = datetime.now(UTC)
    manager.record_tool_result(
        ToolResult(
            ok=True,
            tool="list_open_windows",
            action_id=uuid4(),
            step_id=uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
            data={
                "query": "Calculator",
                "count": 1,
                "windows": [
                    {
                        "handle": 44,
                        "title": "Calculator",
                        "process_id": 55,
                        "application": "CalculatorApp.exe",
                        "minimized": True,
                        "maximized": False,
                    }
                ],
            },
        )
    )

    snapshot = manager.snapshot()
    assert snapshot.last_action is not None
    assert snapshot.last_action["target"] == "Calculator"
    assert snapshot.recently_mentioned_applications[-1] == "Calculator"
    assert snapshot.recently_referenced_windows[-1].handle == 44


def test_application_launch_keeps_stable_target_instead_of_raw_window_title() -> None:
    manager = ConversationManager()
    now = datetime.now(UTC)
    manager.record_tool_result(
        ToolResult(
            ok=True,
            tool="open_application",
            action_id=uuid4(),
            step_id=uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
            data={
                "application": "Calculator",
                "process_id": 20,
                "verified": True,
                "window": {
                    "handle": 44,
                    "title": "Evaluation copy of Calculator",
                    "process_id": 20,
                    "application": "CalculatorApp.exe",
                    "minimized": False,
                    "maximized": False,
                },
            },
        )
    )

    snapshot = manager.snapshot()
    assert snapshot.last_action is not None
    assert snapshot.last_action["target"] == "Calculator"
    assert snapshot.recently_mentioned_applications[-1] == "Calculator"
