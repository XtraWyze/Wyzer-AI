"""Native schemas for model-driven task planning operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from wyzer.models import NativeFunctionDefinition, NativeToolDefinition, ToolArguments


class TaskPlanStepInput(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    success_criteria: str = Field(min_length=1, max_length=500)


class CreateTaskPlanArguments(ToolArguments):
    goal: str = Field(min_length=1, max_length=2_000)
    steps: list[TaskPlanStepInput] = Field(
        min_length=2,
        max_length=12,
        description=(
            "The smallest non-overlapping set of outcome steps, normally 2 to 6. Never add "
            "steps for planning, narration, or final reporting."
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
            "Required as the first and only call when a request needs two or more distinct "
            "computer actions. Silently create the internal plan before any capability call. "
            "Do not use for conversation or a single routine action."
        ),
        "task_step_update": (
            "Update the current task step from tool evidence. Mark verified only after a "
            "successful observation or a tool result explicitly verified the action."
        ),
        "task_plan_revise": (
            "Replace the unfinished portion of the current plan when evidence requires a "
            "different approach. Preserve already verified steps."
        ),
    }
    return [
        NativeToolDefinition(
            function=NativeFunctionDefinition(
                name=name,
                description=descriptions[name],
                parameters=arguments_type.model_json_schema(),
            )
        )
        for name, arguments_type in TASK_ARGUMENT_TYPES.items()
    ]
