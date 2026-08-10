"""Typed state for LLM-authored, evidence-backed task plans."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from wyzer.models.core import FrozenModel, utc_now


class TaskStepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    NEEDS_VERIFICATION = "needs_verification"
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskPlanStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class TaskEvidence(FrozenModel):
    tool: str
    ok: bool
    verification_status: str | None = None
    verification_eligible: bool = False
    summary: str = ""
    recorded_at: datetime = Field(default_factory=utc_now)


class TaskStep(FrozenModel):
    number: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=500)
    success_criteria: str = Field(min_length=1, max_length=500)
    status: TaskStepStatus = TaskStepStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    evidence: list[TaskEvidence] = Field(default_factory=list)
    note: str = Field(default="", max_length=1_000)


class TaskPlan(FrozenModel):
    action_id: UUID
    goal: str = Field(min_length=1, max_length=2_000)
    status: TaskPlanStatus = TaskPlanStatus.ACTIVE
    steps: list[TaskStep] = Field(min_length=1, max_length=25)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revision_reason: str = Field(default="", max_length=1_000)
    active_capabilities: list[str] = Field(default_factory=list)

    @property
    def current_step(self) -> TaskStep | None:
        return next(
            (
                step
                for step in self.steps
                if step.status
                in {
                    TaskStepStatus.IN_PROGRESS,
                    TaskStepStatus.NEEDS_VERIFICATION,
                    TaskStepStatus.FAILED,
                }
            ),
            None,
        )
