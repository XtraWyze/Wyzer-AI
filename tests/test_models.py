from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from wyzer.models import StructuredError, ToolResult


def test_failed_result_requires_error() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ToolResult(
            ok=False,
            tool="echo",
            action_id=uuid4(),
            step_id=uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
        )


def test_result_is_json_serializable() -> None:
    now = datetime.now(UTC)
    result = ToolResult(
        ok=False,
        tool="echo",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        error=StructuredError(code="NOPE", message="No result."),
    )
    assert isinstance(result.model_dump_json(), str)
