"""Model-requested capability discovery and activation contracts."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from wyzer.models import RiskLevel, ToolArguments
from wyzer.tools.base import Tool, ToolContext, ToolExecutionError

LIST_CAPABILITIES_TOOL = "list_tool_capabilities"
ACTIVATE_CAPABILITY_TOOL = "activate_tool_capability"
CAPABILITY_COORDINATION_TOOLS = frozenset({LIST_CAPABILITIES_TOOL, ACTIVATE_CAPABILITY_TOOL})

ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)
ResultT = TypeVar("ResultT", bound=BaseModel)


class ListToolCapabilitiesArguments(ToolArguments):
    pass


class CapabilitySummary(BaseModel):
    name: str
    description: str
    tool_count: int = Field(ge=1)
    active: bool


class ListToolCapabilitiesResult(BaseModel):
    capabilities: list[CapabilitySummary]


class ActivateToolCapabilityArguments(ToolArguments):
    name: str = Field(
        min_length=1,
        max_length=64,
        description="Exact capability name returned by list_tool_capabilities.",
    )


class ActivateToolCapabilityResult(BaseModel):
    name: str
    activated: bool
    visible_tool_count: int = Field(ge=1)
    instruction: str


class CapabilityActivationArguments(ToolArguments):
    pass


class _CoordinatorTool(Tool[ArgumentsT, ResultT], Generic[ArgumentsT, ResultT]):
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: ArgumentsT, context: ToolContext) -> ResultT:
        del arguments, context
        raise ToolExecutionError(
            "COORDINATOR_ONLY",
            "Capability coordination is handled by the conversational orchestrator.",
        )


class ListToolCapabilitiesTool(
    _CoordinatorTool[ListToolCapabilitiesArguments, ListToolCapabilitiesResult]
):
    name = LIST_CAPABILITIES_TOOL
    description = (
        "When the needed action tool is absent, discover optional packs: managed web, local files, "
        "clipboard, desktop typing, screen perception, and diagnostics. Do not substitute a "
        "merely similar visible tool."
    )
    arguments_type = ListToolCapabilitiesArguments
    result_type = ListToolCapabilitiesResult


class ActivateToolCapabilityTool(
    _CoordinatorTool[ActivateToolCapabilityArguments, ActivateToolCapabilityResult]
):
    name = ACTIVATE_CAPABILITY_TOOL
    description = (
        "Make one exact discovered pack's tools available on the next round. Activation does not "
        "perform the user's action; continue by calling the needed new action tool."
    )
    arguments_type = ActivateToolCapabilityArguments
    result_type = ActivateToolCapabilityResult


class CapabilityActivationTool(
    _CoordinatorTool[CapabilityActivationArguments, ActivateToolCapabilityResult]
):
    """One metadata-generated, zero-argument capability activator."""

    arguments_type = CapabilityActivationArguments
    result_type = ActivateToolCapabilityResult

    def __init__(
        self,
        *,
        name: str,
        capability_name: str,
        capability_description: str,
    ) -> None:
        self.name = name
        self.capability_name = capability_name
        self.description = (
            f"Make {capability_description} tools visible on the next round. "
            "Does not perform the user's action."
        )
