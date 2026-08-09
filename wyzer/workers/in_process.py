"""In-process executor intended for tests and development."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from wyzer.models import StructuredError, ToolResult
from wyzer.tools import ToolContext, ToolExecutionError, ToolRegistry
from wyzer.tools.registry import UnavailableToolError, UnknownToolError


class InProcessExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def cancel(self, action_id: UUID) -> bool:
        del action_id
        return False

    async def execute(
        self,
        tool_name: str,
        raw_arguments: dict[str, Any],
        action_id: UUID,
        step_id: UUID,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        started_at = datetime.now(UTC)
        started_counter = time.perf_counter()
        try:
            tool = self._registry.get(tool_name)
            arguments = tool.arguments_type.model_validate(raw_arguments)
            timeout = timeout_seconds or tool.default_timeout_seconds
            output = await asyncio.wait_for(
                asyncio.to_thread(tool.execute, arguments, ToolContext(action_id, step_id)),
                timeout=timeout,
            )
            return self._result(
                True,
                tool_name,
                action_id,
                step_id,
                started_at,
                started_counter,
                data=tool.result_data(output),
                evidence=tool.result_evidence(output),
                warnings=tool.result_warnings(output),
            )
        except TimeoutError:
            error = StructuredError(
                code="TOOL_TIMEOUT",
                message=f"{tool_name} exceeded its execution timeout.",
                retryable=True,
            )
        except UnknownToolError:
            error = StructuredError(
                code="UNKNOWN_TOOL",
                message=f"No registered tool named {tool_name}.",
                retryable=False,
            )
        except UnavailableToolError as exception:
            error = StructuredError(
                code="TOOL_UNAVAILABLE", message=str(exception), retryable=False
            )
        except ValidationError as exception:
            error = StructuredError(
                code="INVALID_TOOL_ARGUMENTS",
                message="Tool arguments did not match the registered schema.",
                retryable=False,
                details={"errors": exception.errors(include_url=False)},
            )
        except ToolExecutionError as exception:
            error = StructuredError(
                code=exception.code,
                message=str(exception),
                retryable=exception.retryable,
                details=exception.details,
            )
        except Exception as exception:  # execution boundary intentionally normalizes failures
            error = StructuredError(
                code="TOOL_EXECUTION_FAILED",
                message=str(exception) or exception.__class__.__name__,
                retryable=False,
                details={"exception_type": exception.__class__.__name__},
            )
        return self._result(
            False, tool_name, action_id, step_id, started_at, started_counter, error=error
        )

    @staticmethod
    def _result(
        ok: bool,
        tool_name: str,
        action_id: UUID,
        step_id: UUID,
        started_at: datetime,
        started_counter: float,
        data: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        error: StructuredError | None = None,
    ) -> ToolResult:
        finished_at = datetime.now(UTC)
        duration_ms = max(0, round((time.perf_counter() - started_counter) * 1000))
        return ToolResult(
            ok=ok,
            tool=tool_name,
            action_id=action_id,
            step_id=step_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            data=data,
            evidence=evidence or {},
            warnings=warnings or [],
            error=error,
        )
