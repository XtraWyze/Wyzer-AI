"""Registry-visible coding coordination proxies executed only by Orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, Field

from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments
from wyzer.tools import SimpleToolPack, Tool, ToolContext, ToolExecutionError


class CodingAgentStartArguments(ToolArguments):
    workspace: str = Field(
        min_length=1,
        max_length=1_000,
        description="Exact existing or explicitly requested new project directory.",
    )
    task: str = Field(min_length=1, max_length=20_000)
    create_workspace: bool = Field(
        default=False,
        description="Create this exact directory only when the user requested a new project there.",
    )


class CodingAgentMessageArguments(ToolArguments):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(
        default=None, description="Required when more than one coding session exists."
    )


class CodingAgentSessionArguments(ToolArguments):
    session_id: str | None = Field(
        default=None, description="Required when more than one coding session exists."
    )


class CodingProxyResult(BaseModel):
    accepted: bool = True


class _CodingProxyTool(Tool[ToolArguments, CodingProxyResult]):
    result_type = CodingProxyResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.NEVER
    default_timeout_seconds = 600

    def execute(self, arguments: ToolArguments, context: ToolContext) -> CodingProxyResult:
        del arguments, context
        raise ToolExecutionError(
            "CODING_PROXY_MAIN_PROCESS_ONLY",
            "Coding-agent coordination must be executed by the main-process manager.",
        )


class CodingAgentStartTool(_CodingProxyTool):
    name = "coding_agent_start"
    description = "Start coding in an exact workspace; can create a user-requested new project directory."
    arguments_type = CodingAgentStartArguments


class CodingAgentMessageTool(_CodingProxyTool):
    name = "coding_agent_message"
    description = "Continue, retry, improve, run, test, or fix work in a retained coding session."
    arguments_type = CodingAgentMessageArguments


class CodingAgentStatusTool(_CodingProxyTool):
    name = "coding_agent_status"
    description = "Report where prior coding work is, what changed, and its current state."
    arguments_type = CodingAgentSessionArguments
    risk_level = RiskLevel.LOW
    read_only = True


class CodingAgentCancelTool(_CodingProxyTool):
    name = "coding_agent_cancel"
    description = "Only when explicitly asked to stop/cancel coding, interrupt its active operation."
    arguments_type = CodingAgentSessionArguments


def create_coding_agent_pack() -> SimpleToolPack:
    return SimpleToolPack(
        "coding_agent",
        (
            CodingAgentStartTool,
            CodingAgentMessageTool,
            CodingAgentStatusTool,
            CodingAgentCancelTool,
        ),
        "Create, change, run, test, and debug code as one action, including new project directories.",
        "coding_agent",
    )
