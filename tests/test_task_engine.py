from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from wyzer.models import StructuredError, ToolResult
from wyzer.tasks import TaskPlanStatus, TaskStateError, TaskStateStore, TaskStepStatus


def result(*, ok: bool, read_only: bool = False) -> tuple[ToolResult, bool]:
    now = datetime.now(UTC)
    return (
        ToolResult(
            ok=ok,
            tool="inspect" if read_only else "change",
            action_id=uuid4(),
            step_id=uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
            data={} if ok else None,
            error=None if ok else StructuredError(code="FAILED", message="failed"),
        ),
        read_only,
    )


def steps() -> list[dict[str, str]]:
    return [
        {"description": "Change the setting", "success_criteria": "Setting changed"},
        {"description": "Confirm the result", "success_criteria": "Result is visible"},
    ]


def test_mutation_requires_verification_before_step_completion() -> None:
    store = TaskStateStore()
    store.create(uuid4(), "Change and confirm", steps())
    mutation, read_only = result(ok=True)
    store.record_tool_result(mutation, read_only=read_only)

    assert store.snapshot().current_step.status == TaskStepStatus.NEEDS_VERIFICATION  # type: ignore[union-attr]
    with pytest.raises(TaskStateError, match="no successful read-only observation"):
        store.update_step(1, TaskStepStatus.VERIFIED)

    observation, read_only = result(ok=True, read_only=True)
    store.record_tool_result(observation, read_only=read_only)
    plan = store.update_step(1, TaskStepStatus.VERIFIED)
    assert plan.steps[0].status == TaskStepStatus.VERIFIED
    assert plan.steps[1].status == TaskStepStatus.IN_PROGRESS


def test_completed_plan_requires_every_step_to_be_verified() -> None:
    store = TaskStateStore()
    store.create(uuid4(), "Observe twice", steps())
    for number in (1, 2):
        observation, read_only = result(ok=True, read_only=True)
        store.record_tool_result(observation, read_only=read_only)
        plan = store.update_step(number, TaskStepStatus.VERIFIED)
    assert plan.status == TaskPlanStatus.COMPLETED
    assert store.context() is None
    assert store.summary().startswith("Last task: Observe twice (completed)")


def test_only_active_plan_is_exposed_as_model_context() -> None:
    store = TaskStateStore()
    store.create(uuid4(), "Keep working", steps())

    assert store.context() is not None

    store.pause()
    assert store.context() is None

    store.resume()
    assert store.context() is not None

    store.cancel()
    assert store.context() is None


def test_only_current_step_can_be_updated() -> None:
    store = TaskStateStore()
    store.create(uuid4(), "Stay ordered", steps())

    with pytest.raises(TaskStateError, match="step 2 is not the current step"):
        store.update_step(2, TaskStepStatus.BLOCKED)


def test_repeated_failures_block_plan_at_retry_limit() -> None:
    store = TaskStateStore(maximum_retries_per_step=1)
    store.create(uuid4(), "Try safely", steps())
    for _ in range(2):
        failure, read_only = result(ok=False)
        store.record_tool_result(failure, read_only=read_only)
    plan = store.snapshot()
    assert plan is not None
    assert plan.status == TaskPlanStatus.BLOCKED
    assert plan.steps[0].status == TaskStepStatus.BLOCKED


def test_active_plan_is_recovered_as_paused(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    action_id = uuid4()
    TaskStateStore(path).create(action_id, "Recover me", steps())

    recovered = TaskStateStore(path).snapshot()

    assert recovered is not None
    assert recovered.action_id == action_id
    assert recovered.status == TaskPlanStatus.PAUSED


def test_active_capabilities_persist_with_planned_task(tmp_path: Path) -> None:
    path = tmp_path / "task.json"
    store = TaskStateStore(path)
    plan = store.create(
        uuid4(),
        "Use files",
        steps(),
        active_capabilities=("files",),
    )
    store.activate_capability("browser")

    assert plan.active_capabilities == ["files"]
    recovered = TaskStateStore(path).snapshot()
    assert recovered is not None
    assert recovered.active_capabilities == ["browser", "files"]
