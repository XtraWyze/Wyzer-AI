"""Small deterministic test doubles."""

import time

from pydantic import BaseModel

from wyzer.models import (
    ChatMessage,
    ConfirmationMode,
    NativeFunctionCall,
    NativeToolCall,
    ProviderChatResponse,
    RiskLevel,
    ToolArguments,
)
from wyzer.tools import Tool, ToolContext


class EchoArguments(ToolArguments):
    message: str


class EchoData(BaseModel):
    echoed: str


class EchoTool(Tool[EchoArguments, EchoData]):
    name = "echo"
    description = "Echo validated text for executor tests."
    arguments_type = EchoArguments
    result_type = EchoData
    risk_level = RiskLevel.LOW
    read_only = True
    default_timeout_seconds = 1.0

    def execute(self, arguments: EchoArguments, context: ToolContext) -> EchoData:
        del context
        return EchoData(echoed=arguments.message)


class FailingTool(EchoTool):
    name = "failing"

    def execute(self, arguments: EchoArguments, context: ToolContext) -> EchoData:
        del arguments, context
        raise RuntimeError("expected failure")


class VerifiedActionData(BaseModel):
    message: str
    changed: bool = True
    evidence: dict[str, str] = {
        "verification_status": "verified",
        "predicate": "requested_action_observed",
    }


class VerifiedActionTool(Tool[EchoArguments, VerifiedActionData]):
    name = "verified_action"
    description = "Perform a deterministic action with explicit verification evidence."
    arguments_type = EchoArguments
    result_type = VerifiedActionData
    risk_level = RiskLevel.LOW
    read_only = False

    def execute(self, arguments: EchoArguments, context: ToolContext) -> VerifiedActionData:
        del context
        return VerifiedActionData(message=arguments.message)


class ApplicationArguments(ToolArguments):
    application: str


class ApplicationData(BaseModel):
    launched: bool


class OpenApplicationTool(Tool[ApplicationArguments, ApplicationData]):
    name = "open_application"
    description = "Open an application for deterministic tests."
    arguments_type = ApplicationArguments
    result_type = ApplicationData
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: ApplicationArguments, context: ToolContext) -> ApplicationData:
        del arguments, context
        return ApplicationData(launched=True)


class SlowEchoTool(EchoTool):
    name = "slow_echo"

    def execute(self, arguments: EchoArguments, context: ToolContext) -> EchoData:
        time.sleep(0.05)
        return super().execute(arguments, context)


class ConsequentialEchoTool(EchoTool):
    name = "send_message"
    read_only = False
    confirmation = ConfirmationMode.ALWAYS


class RefreshFileIndexArguments(ToolArguments):
    include_content: bool = True


class RefreshFileIndexData(BaseModel):
    files: int
    content_files: int
    skipped: int
    errors: int
    content_read: bool


class FakeRefreshFileIndexTool(Tool[RefreshFileIndexArguments, RefreshFileIndexData]):
    name = "refresh_file_index"
    description = "Refresh a deterministic test file index."
    arguments_type = RefreshFileIndexArguments
    result_type = RefreshFileIndexData
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: RefreshFileIndexArguments, context: ToolContext
    ) -> RefreshFileIndexData:
        del context
        return RefreshFileIndexData(
            files=530_746,
            content_files=12_345,
            skipped=7,
            errors=0,
            content_read=arguments.include_content,
        )


def text_response(text: str) -> ProviderChatResponse:
    return ProviderChatResponse(message=ChatMessage(role="assistant", content=text))


def tool_response(*calls: tuple[str, dict[str, object]]) -> ProviderChatResponse:
    return ProviderChatResponse(
        message=ChatMessage(
            role="assistant",
            tool_calls=[
                NativeToolCall(
                    id=f"call_{index}",
                    function=NativeFunctionCall(name=name, arguments=arguments),
                )
                for index, (name, arguments) in enumerate(calls)
            ],
        )
    )
