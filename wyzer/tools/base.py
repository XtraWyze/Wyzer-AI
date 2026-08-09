"""Tool protocol and execution context."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments, ToolDefinition

ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)
ResultDataT = TypeVar("ResultDataT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ToolContext:
    action_id: UUID
    step_id: UUID


class ToolExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        super().__init__(message)


class Tool(ABC, Generic[ArgumentsT, ResultDataT]):
    """Stateless deterministic capability exposed through the registry."""

    name: str
    description: str
    arguments_type: type[ArgumentsT]
    result_type: type[ResultDataT]
    risk_level: RiskLevel
    read_only: bool
    confirmation: ConfirmationMode = ConfirmationMode.NEVER
    default_timeout_seconds: float = 15.0
    available: bool = True
    unavailable_reason: str | None = None
    llm_visible: bool = True

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            arguments_schema=self.arguments_type.model_json_schema(),
            result_schema=self.result_type.model_json_schema(),
            risk_level=self.risk_level,
            read_only=self.read_only,
            confirmation=self.confirmation,
            default_timeout_seconds=self.default_timeout_seconds,
            available=self.available,
            unavailable_reason=self.unavailable_reason,
        )

    def result_data(self, result: ResultDataT) -> dict[str, Any]:
        return result.model_dump(mode="json", exclude={"evidence", "warnings"})

    def result_evidence(self, result: ResultDataT) -> dict[str, Any]:
        evidence = getattr(result, "evidence", {})
        return dict(evidence) if isinstance(evidence, dict) else {}

    def result_warnings(self, result: ResultDataT) -> list[str]:
        warnings = getattr(result, "warnings", [])
        return list(warnings) if isinstance(warnings, list) else []

    @abstractmethod
    def execute(self, arguments: ArgumentsT, context: ToolContext) -> ResultDataT:
        """Perform the capability or raise a structured/ordinary exception."""
