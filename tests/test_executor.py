import asyncio
from uuid import uuid4

from tests.fakes import EchoTool, FailingTool
from wyzer.tools import ToolRegistry
from wyzer.workers import InProcessExecutor


def test_executor_returns_standard_success() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    result = asyncio.run(
        InProcessExecutor(registry).execute("echo", {"message": "hello"}, uuid4(), uuid4())
    )
    assert result.ok is True
    assert result.data == {"echoed": "hello"}


def test_executor_normalizes_invalid_arguments() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    result = asyncio.run(InProcessExecutor(registry).execute("echo", {}, uuid4(), uuid4()))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "INVALID_TOOL_ARGUMENTS"


def test_executor_normalizes_expected_failure() -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())
    result = asyncio.run(
        InProcessExecutor(registry).execute("failing", {"message": "hello"}, uuid4(), uuid4())
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "TOOL_EXECUTION_FAILED"


def test_executor_rejects_unknown_tool() -> None:
    result = asyncio.run(
        InProcessExecutor(ToolRegistry()).execute("imaginary", {}, uuid4(), uuid4())
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "UNKNOWN_TOOL"
