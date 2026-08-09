"""Executor protocol shared by in-process, fake, and worker implementations."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from wyzer.models import ToolResult


class ToolExecutor(Protocol):
    async def execute(
        self,
        tool_name: str,
        raw_arguments: dict[str, Any],
        action_id: UUID,
        step_id: UUID,
        timeout_seconds: float | None = None,
    ) -> ToolResult: ...

    def cancel(self, action_id: UUID) -> bool: ...
