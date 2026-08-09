"""Thread-safe task state with optional crash-safe JSON persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import UUID

from wyzer.models import ToolResult
from wyzer.tasks.models import (
    TaskEvidence,
    TaskPlan,
    TaskPlanStatus,
    TaskStep,
    TaskStepStatus,
)


class TaskStateError(ValueError):
    pass


class TaskStateStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        maximum_steps: int = 12,
        maximum_retries_per_step: int = 2,
    ) -> None:
        self.path = path
        self.maximum_steps = maximum_steps
        self.maximum_retries_per_step = maximum_retries_per_step
        self._lock = RLock()
        self._plan = self._load()
        if self._plan is not None and self._plan.status == TaskPlanStatus.ACTIVE:
            self._plan = self._plan.model_copy(
                update={"status": TaskPlanStatus.PAUSED, "updated_at": datetime.now(UTC)}
            )
            self._save()

    def snapshot(self) -> TaskPlan | None:
        with self._lock:
            return self._plan.model_copy(deep=True) if self._plan is not None else None

    def create(self, action_id: UUID, goal: str, steps: list[dict[str, str]]) -> TaskPlan:
        if not 1 <= len(steps) <= self.maximum_steps:
            raise TaskStateError(f"a plan requires 1 to {self.maximum_steps} steps")
        typed = [
            TaskStep(
                number=index,
                description=str(step.get("description") or "").strip(),
                success_criteria=str(step.get("success_criteria") or "").strip(),
                status=(TaskStepStatus.IN_PROGRESS if index == 1 else TaskStepStatus.PENDING),
            )
            for index, step in enumerate(steps, 1)
        ]
        with self._lock:
            if self._plan is not None and self._plan.action_id == action_id:
                raise TaskStateError(
                    "this action already has a plan; update it or revise its unfinished steps"
                )
            self._plan = TaskPlan(action_id=action_id, goal=goal.strip(), steps=typed)
            self._save()
            return self._plan.model_copy(deep=True)

    def record_tool_result(self, result: ToolResult, *, read_only: bool) -> None:
        with self._lock:
            plan = self._plan
            if plan is None or plan.status != TaskPlanStatus.ACTIVE:
                return
            current = plan.current_step
            if current is None:
                return
            verification_status = result.evidence.get("verification_status")
            eligible = result.ok and (read_only or verification_status == "verified")
            summary = (
                "success"
                if result.ok
                else (result.error.message if result.error is not None else "failed")
            )
            evidence = TaskEvidence(
                tool=result.tool,
                ok=result.ok,
                verification_status=(
                    str(verification_status) if verification_status is not None else None
                ),
                verification_eligible=eligible,
                summary=summary[:500],
            )
            status = current.status
            if not result.ok:
                prior_failures = sum(not item.ok for item in current.evidence)
                status = (
                    TaskStepStatus.BLOCKED
                    if prior_failures >= self.maximum_retries_per_step
                    else TaskStepStatus.FAILED
                )
            elif not eligible:
                status = TaskStepStatus.NEEDS_VERIFICATION
            updated = current.model_copy(
                update={
                    "status": status,
                    "attempts": current.attempts + 1,
                    "evidence": [*current.evidence, evidence],
                }
            )
            self._replace_step(updated)
            if status == TaskStepStatus.BLOCKED:
                assert self._plan is not None
                self._plan = self._plan.model_copy(
                    update={"status": TaskPlanStatus.BLOCKED, "updated_at": datetime.now(UTC)}
                )
                self._save()

    def update_step(self, number: int, status: TaskStepStatus, note: str = "") -> TaskPlan:
        with self._lock:
            plan = self._require_plan()
            if plan.status != TaskPlanStatus.ACTIVE:
                raise TaskStateError(
                    f"the plan is {plan.status.value}; do not update steps in this state"
                )
            step = self._step(number)
            current = plan.current_step
            if current is None or current.number != number:
                raise TaskStateError(
                    f"step {number} is not the current step; update step "
                    f"{current.number if current is not None else 'none'}"
                )
            if status == TaskStepStatus.VERIFIED and not any(
                item.verification_eligible for item in step.evidence
            ):
                raise TaskStateError(
                    "that step has no successful read-only observation or verified action evidence"
                )
            updated = step.model_copy(update={"status": status, "note": note.strip()})
            self._replace_step(updated, save=False)
            assert self._plan is not None
            steps = list(self._plan.steps)
            plan_status = self._plan.status
            if status == TaskStepStatus.VERIFIED:
                pending_index = next(
                    (
                        index
                        for index, item in enumerate(steps)
                        if item.status == TaskStepStatus.PENDING
                    ),
                    None,
                )
                if pending_index is None:
                    plan_status = TaskPlanStatus.COMPLETED
                else:
                    steps[pending_index] = steps[pending_index].model_copy(
                        update={"status": TaskStepStatus.IN_PROGRESS}
                    )
            elif status == TaskStepStatus.BLOCKED:
                plan_status = TaskPlanStatus.BLOCKED
            self._plan = self._plan.model_copy(
                update={
                    "steps": steps,
                    "status": plan_status,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._save()
            return self._plan.model_copy(deep=True)

    def revise(self, reason: str, remaining_steps: list[dict[str, str]]) -> TaskPlan:
        if not remaining_steps or len(remaining_steps) > self.maximum_steps:
            raise TaskStateError(f"a revision requires 1 to {self.maximum_steps} remaining steps")
        with self._lock:
            plan = self._require_plan()
            if plan.status not in {TaskPlanStatus.ACTIVE, TaskPlanStatus.BLOCKED}:
                raise TaskStateError(
                    f"the plan is {plan.status.value}; it cannot be revised in this state"
                )
            verified = [step for step in plan.steps if step.status == TaskStepStatus.VERIFIED]
            if len(verified) + len(remaining_steps) > self.maximum_steps:
                raise TaskStateError(
                    f"the revised plan cannot exceed {self.maximum_steps} total steps"
                )
            new_steps = list(verified)
            for offset, raw in enumerate(remaining_steps, len(verified) + 1):
                new_steps.append(
                    TaskStep(
                        number=offset,
                        description=str(raw.get("description") or "").strip(),
                        success_criteria=str(raw.get("success_criteria") or "").strip(),
                        status=(
                            TaskStepStatus.IN_PROGRESS
                            if offset == len(verified) + 1
                            else TaskStepStatus.PENDING
                        ),
                    )
                )
            self._plan = plan.model_copy(
                update={
                    "steps": new_steps,
                    "status": TaskPlanStatus.ACTIVE,
                    "revision": plan.revision + 1,
                    "revision_reason": reason.strip(),
                    "updated_at": datetime.now(UTC),
                }
            )
            self._save()
            return self._plan.model_copy(deep=True)

    def pause(self) -> TaskPlan:
        return self._set_plan_status(TaskPlanStatus.PAUSED)

    def resume(self, action_id: UUID | None = None) -> TaskPlan:
        with self._lock:
            plan = self._require_plan()
            updates: dict[str, object] = {
                "status": TaskPlanStatus.ACTIVE,
                "updated_at": datetime.now(UTC),
            }
            if action_id is not None:
                updates["action_id"] = action_id
            self._plan = plan.model_copy(update=updates)
            self._save()
            return self._plan.model_copy(deep=True)

    def cancel(self) -> TaskPlan:
        return self._set_plan_status(TaskPlanStatus.CANCELLED)

    def summary(self) -> str:
        plan = self.snapshot()
        if plan is None:
            return "There is no saved task plan."
        lines = [f"Task: {plan.goal} ({plan.status.value})"]
        for step in plan.steps:
            lines.append(f"{step.number}. [{step.status.value}] {step.description}")
        return "\n".join(lines)

    def context(self) -> dict[str, object] | None:
        plan = self.snapshot()
        if plan is None or plan.status in {
            TaskPlanStatus.COMPLETED,
            TaskPlanStatus.CANCELLED,
        }:
            return None
        return plan.model_dump(mode="json")

    def _set_plan_status(self, status: TaskPlanStatus) -> TaskPlan:
        with self._lock:
            plan = self._require_plan()
            self._plan = plan.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})
            self._save()
            return self._plan.model_copy(deep=True)

    def _require_plan(self) -> TaskPlan:
        if self._plan is None:
            raise TaskStateError("there is no task plan")
        return self._plan

    def _step(self, number: int) -> TaskStep:
        plan = self._require_plan()
        try:
            return next(step for step in plan.steps if step.number == number)
        except StopIteration as error:
            raise TaskStateError(f"task step {number} does not exist") from error

    def _replace_step(self, replacement: TaskStep, *, save: bool = True) -> None:
        plan = self._require_plan()
        steps = [replacement if step.number == replacement.number else step for step in plan.steps]
        self._plan = plan.model_copy(update={"steps": steps, "updated_at": datetime.now(UTC)})
        if save:
            self._save()

    def _load(self) -> TaskPlan | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            return TaskPlan.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self) -> None:
        if self.path is None or self._plan is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
