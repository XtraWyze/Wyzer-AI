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
        "Discover optional browser, clipboard, desktop UI, diagnostics, files, and screen-perception "
        "packs when the exact action tool is absent; do not substitute a visible tool."
    )
    arguments_type = ListToolCapabilitiesArguments
    result_type = ListToolCapabilitiesResult


class ActivateToolCapabilityTool(
    _CoordinatorTool[ActivateToolCapabilityArguments, ActivateToolCapabilityResult]
):
    name = ACTIVATE_CAPABILITY_TOOL
    description = (
        "Activate one exact pack name returned by capability discovery for this action or task; "
        "use its newly visible tools on the next round."
    )
    arguments_type = ActivateToolCapabilityArguments
    result_type = ActivateToolCapabilityResult
