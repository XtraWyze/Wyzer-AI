"""Native schemas for model-driven task planning operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from wyzer.models import NativeFunctionDefinition, NativeToolDefinition, ToolArguments
from wyzer.tools.schema import model_parameters


class TaskPlanStepInput(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    success_criteria: str = Field(min_length=1, max_length=500)


class CreateTaskPlanArguments(ToolArguments):
    goal: str = Field(min_length=1, max_length=2_000)
    steps: list[TaskPlanStepInput] = Field(
        min_length=2,
        max_length=12,
        description=(
            "Smallest non-overlapping outcome steps, normally 2-6; exclude narration/reporting."
        ),
    )


class UpdateTaskStepArguments(ToolArguments):
    step_number: int = Field(ge=1, le=25)
    status: Literal["verified", "failed", "blocked"]
    note: str = Field(default="", max_length=1_000)


class ReviseTaskPlanArguments(ToolArguments):
    reason: str = Field(min_length=1, max_length=1_000)
    remaining_steps: list[TaskPlanStepInput] = Field(min_length=1, max_length=12)


TASK_ARGUMENT_TYPES: dict[str, type[ToolArguments]] = {
    "task_plan_create": CreateTaskPlanArguments,
    "task_step_update": UpdateTaskStepArguments,
    "task_plan_revise": ReviseTaskPlanArguments,
}


def task_native_tools() -> list[NativeToolDefinition]:
    descriptions = {
        "task_plan_create": (
            "First and only call for requests needing 2+ distinct computer actions; not for one action."
        ),
        "task_step_update": (
            "Update the current step from evidence; verified requires observed or explicit success."
        ),
        "task_plan_revise": (
            "Replace unfinished steps when evidence requires a new approach."
        ),
    }
    return [
        NativeToolDefinition(
            function=NativeFunctionDefinition(
                name=name,
                description=descriptions[name],
                parameters=model_parameters(arguments_type),
            )
        )
        for name, arguments_type in TASK_ARGUMENT_TYPES.items()
    ]
