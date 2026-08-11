from datetime import UTC, datetime
from uuid import uuid4

from wyzer.app.tool_context import ToolResultContextBuilder
from wyzer.models import StructuredError, ToolResult


def test_large_binary_and_internal_payloads_are_excluded_from_model_context() -> None:
    now = datetime.now(UTC)
    result = ToolResult(
        ok=True,
        tool="read_text_file",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=1,
        data={
            "text": "useful",
            "binary": "x" * 10000,
            "screenshot_evidence": {"path": "secret.png", "sha256": "a" * 64},
        },
        evidence={"path": "evidence/large.png", "sha256": "b" * 64},
    )

    context = ToolResultContextBuilder(512).build(result)

    assert "useful" in context
    assert "binary" not in context
    assert "screenshot" not in context
    assert "secret.png" not in context
    assert len(context) <= 512


def test_model_context_prefers_stable_application_target_over_raw_window_title() -> None:
    now = datetime.now(UTC)
    result = ToolResult(
        ok=True,
        tool="open_application",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        data={
            "application": "Calculator",
            "verified": True,
            "window": {
                "handle": 44,
                "title": "Evaluation copy of Calculator",
                "process_id": 20,
                "application": "CalculatorApp.exe",
            },
        },
    )

    context = ToolResultContextBuilder().build(result)

    assert '"response_target":"Calculator"' in context
    assert "Evaluation copy" not in context


def test_model_context_omits_raw_monitor_ids_but_keeps_topology_labels() -> None:
    now = datetime.now(UTC)
    result = ToolResult(
        ok=True,
        tool="move_named_window_to_monitor",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        data={
            "target": "Notepad",
            "target_monitor": {
                "monitor_id": "monitor:65659",
                "device_name": r"\\.\DISPLAY2",
                "number": 2,
                "label": "monitor 2",
                "relative_position": "right",
            },
            "window": {
                "handle": 44,
                "title": "Notes",
                "process_id": 20,
                "application": "notepad.exe",
                "monitor_id": "monitor:65659",
            },
        },
    )

    context = ToolResultContextBuilder().build(result)

    assert "monitor:65659" not in context
    assert "monitor 2" in context
    assert '"response_target":"Notepad"' in context


def test_invalid_task_context_preserves_internal_details_but_guides_model_recovery() -> None:
    now = datetime.now(UTC)
    result = ToolResult(
        ok=False,
        tool="task_plan_create",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        error=StructuredError(
            code="INVALID_TASK_ARGUMENTS",
            message="Task arguments did not match the planning schema.",
            retryable=True,
            details={"errors": [{"type": "missing", "loc": ["goal"]}]},
        ),
    )

    context = ToolResultContextBuilder().build(result)

    assert result.error is not None and result.error.details["errors"]
    assert "Do not expose this schema error" in context
    assert "ask the user for planning fields" in context
    assert '"details"' not in context
    assert '"type":"missing"' not in context
