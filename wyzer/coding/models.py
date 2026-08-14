"""Typed coding-agent state and configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from wyzer.models import ChatMessage


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CodingSessionStatus(StrEnum):
    RUNNING = "running"
    IDLE = "idle"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CodingAgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    maximum_rounds: int = Field(default=12, ge=1, le=50)
    maximum_history_messages: int = Field(default=40, ge=12, le=200)
    tool_result_context_characters: int = Field(default=6_000, ge=512, le=100_000)
    command_timeout_seconds: float = Field(default=60, gt=0, le=600)
    maximum_output_characters: int = Field(default=12_000, ge=1_000, le=100_000)
    max_response_tokens: int = Field(default=1_024, ge=64, le=4_096)


class CodingSession(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    session_id: UUID = Field(default_factory=uuid4)
    workspace: Path
    messages: list[ChatMessage] = Field(default_factory=list)
    status: CodingSessionStatus = CodingSessionStatus.IDLE
    current_task: str | None = None
    last_task: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    last_summary: str | None = None
    commands_run: list[dict[str, Any]] = Field(default_factory=list)
    last_verification: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    _cancelled: bool = PrivateAttr(default=False)

    def touch(self) -> None:
        self.updated_at = _utc_now()
