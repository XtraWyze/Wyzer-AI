import asyncio
from uuid import uuid4

from tests.worker_fixtures import create_worker_registry
from wyzer.models import ToolResult
from wyzer.workers import IsolatedExecutor, WorkerHealth


def test_isolated_worker_returns_json_safe_result() -> None:
    executor = IsolatedExecutor(create_worker_registry)

    result = asyncio.run(executor.execute("echo", {"message": "hello"}, uuid4(), uuid4()))

    assert result.ok is True
    assert result.data == {"echoed": "hello"}
    assert executor.health().completed == 1


def test_isolated_worker_timeout_terminates_process() -> None:
    executor = IsolatedExecutor(create_worker_registry, default_timeout_seconds=0.1)

    result = asyncio.run(executor.execute("hanging", {"message": "wait"}, uuid4(), uuid4()))

    assert result.ok is False
    assert result.error is not None and result.error.code == "TOOL_TIMEOUT"
    assert executor.health().active == 0
    assert executor.health().timed_out == 1


def test_isolated_worker_honors_per_tool_timeout_override() -> None:
    executor = IsolatedExecutor(
        create_worker_registry,
        default_timeout_seconds=0.01,
        tool_timeouts={"echo": 3.0},
    )

    result = asyncio.run(executor.execute("echo", {"message": "hello"}, uuid4(), uuid4()))

    assert result.ok is True


def test_isolated_worker_recovers_after_hard_crash() -> None:
    executor = IsolatedExecutor(create_worker_registry)

    crashed = asyncio.run(executor.execute("crashing", {"message": "boom"}, uuid4(), uuid4()))
    recovered = asyncio.run(executor.execute("echo", {"message": "okay"}, uuid4(), uuid4()))

    assert crashed.error is not None and crashed.error.code == "WORKER_CRASHED"
    assert recovered.ok is True
    assert executor.health().crashed == 1


def test_isolated_worker_cancels_active_action() -> None:
    async def scenario() -> tuple[ToolResult, WorkerHealth]:
        executor = IsolatedExecutor(create_worker_registry)
        action_id = uuid4()
        task = asyncio.create_task(
            executor.execute("hanging", {"message": "wait"}, action_id, uuid4())
        )
        for _ in range(100):
            if executor.health().active:
                break
            await asyncio.sleep(0.01)
        assert executor.cancel(action_id) is True
        result = await task
        return result, executor.health()

    result, health = asyncio.run(scenario())

    assert result.error is not None and result.error.code == "WORKER_CANCELLED"
    assert health.active == 0
