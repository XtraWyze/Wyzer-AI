"""Spawn-safe, hard-cancellable tool execution in isolated child processes."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from wyzer.models import StructuredError, ToolResult
from wyzer.tools import ToolRegistry
from wyzer.workers.in_process import InProcessExecutor

RegistryFactory = Callable[[], ToolRegistry]


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    active: int
    started: int
    completed: int
    timed_out: int
    cancelled: int
    crashed: int
    last_error: str | None = None


def _worker_entry(
    result_path: str,
    registry_factory: RegistryFactory,
    tool_name: str,
    raw_arguments: dict[str, Any],
    action_id: UUID,
    step_id: UUID,
    timeout_seconds: float,
) -> None:
    target = Path(result_path)
    temporary = target.with_suffix(".tmp")
    try:
        registry = registry_factory()
        result = asyncio.run(
            InProcessExecutor(registry).execute(
                tool_name,
                raw_arguments,
                action_id,
                step_id,
                timeout_seconds=timeout_seconds,
            )
        )
        temporary.write_text(result.model_dump_json(), encoding="utf-8")
        os.replace(temporary, target)
    except BaseException as error:
        result = _failure_result(
            tool_name,
            action_id,
            step_id,
            "WORKER_CRASHED",
            str(error) or error.__class__.__name__,
            details={"exception_type": error.__class__.__name__},
        )
        with suppress(Exception):
            temporary.write_text(result.model_dump_json(), encoding="utf-8")
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class IsolatedExecutor:
    def __init__(
        self,
        registry_factory: RegistryFactory,
        *,
        maximum_workers: int = 2,
        default_timeout_seconds: float = 15,
        tool_timeouts: Mapping[str, float] | None = None,
        ipc_directory: Path = Path(".wyzer/worker-ipc"),
    ) -> None:
        if maximum_workers < 1:
            raise ValueError("maximum_workers must be positive")
        self._factory = registry_factory
        self._context = multiprocessing.get_context("spawn")
        self._semaphore = asyncio.Semaphore(maximum_workers)
        self._default_timeout = default_timeout_seconds
        self._tool_timeouts = dict(tool_timeouts or {})
        self._ipc_directory = ipc_directory.resolve()
        self._ipc_directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._active: dict[UUID, dict[UUID, BaseProcess]] = {}
        self._cancelled_actions: set[UUID] = set()
        self._started = self._completed = self._timed_out = 0
        self._cancelled = self._crashed = 0
        self._last_error: str | None = None

    async def execute(
        self,
        tool_name: str,
        raw_arguments: dict[str, Any],
        action_id: UUID,
        step_id: UUID,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        timeout = timeout_seconds or self._tool_timeouts.get(tool_name, self._default_timeout)
        async with self._semaphore:
            result_path = self._ipc_directory / f"{action_id}-{step_id}-{uuid4().hex}.json"
            process = self._context.Process(
                target=_worker_entry,
                args=(
                    str(result_path),
                    self._factory,
                    tool_name,
                    raw_arguments,
                    action_id,
                    step_id,
                    timeout,
                ),
                daemon=True,
                name=f"wyzer-tool-{tool_name}",
            )
            started = time.monotonic()
            process_started = False
            try:
                process.start()
                process_started = True
                with self._lock:
                    self._active.setdefault(action_id, {})[step_id] = process
                    self._started += 1
                while True:
                    if result_path.is_file():
                        result = ToolResult.model_validate_json(
                            result_path.read_text(encoding="utf-8")
                        )
                        with self._lock:
                            self._completed += 1
                        return result
                    if not process.is_alive():
                        with self._lock:
                            cancelled = action_id in self._cancelled_actions
                            if cancelled:
                                self._cancelled += 1
                                code, message = "WORKER_CANCELLED", "The tool call was cancelled."
                            else:
                                self._crashed += 1
                                code, message = (
                                    "WORKER_CRASHED",
                                    f"The tool worker exited with code {process.exitcode}.",
                                )
                                self._last_error = message
                        return _failure_result(tool_name, action_id, step_id, code, message)
                    if time.monotonic() - started >= timeout:
                        self._terminate(process)
                        with self._lock:
                            self._timed_out += 1
                            self._last_error = f"{tool_name} timed out"
                        return _failure_result(
                            tool_name,
                            action_id,
                            step_id,
                            "TOOL_TIMEOUT",
                            f"{tool_name} exceeded its execution timeout and was terminated.",
                            retryable=True,
                        )
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self._terminate(process)
                raise
            except Exception as error:
                self._terminate(process)
                with self._lock:
                    self._crashed += 1
                    self._last_error = str(error)
                return _failure_result(
                    tool_name,
                    action_id,
                    step_id,
                    "WORKER_START_FAILED",
                    str(error) or error.__class__.__name__,
                )
            finally:
                result_path.unlink(missing_ok=True)
                result_path.with_suffix(".tmp").unlink(missing_ok=True)
                if process_started:
                    if process.is_alive():
                        self._terminate(process)
                    else:
                        process.join(timeout=0.2)
                    process.close()
                with self._lock:
                    action = self._active.get(action_id)
                    if action is not None:
                        action.pop(step_id, None)
                        if not action:
                            self._active.pop(action_id, None)
                            self._cancelled_actions.discard(action_id)

    def cancel(self, action_id: UUID) -> bool:
        with self._lock:
            processes = list(self._active.get(action_id, {}).values())
            if not processes:
                return False
            self._cancelled_actions.add(action_id)
        for process in processes:
            self._terminate(process)
        return True

    def health(self) -> WorkerHealth:
        with self._lock:
            return WorkerHealth(
                active=sum(len(items) for items in self._active.values()),
                started=self._started,
                completed=self._completed,
                timed_out=self._timed_out,
                cancelled=self._cancelled,
                crashed=self._crashed,
                last_error=self._last_error,
            )

    @staticmethod
    def _terminate(process: BaseProcess) -> None:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=2)


def _failure_result(
    tool_name: str,
    action_id: UUID,
    step_id: UUID,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> ToolResult:
    now = datetime.now(UTC)
    return ToolResult(
        ok=False,
        tool=tool_name,
        action_id=action_id,
        step_id=step_id,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        error=StructuredError(
            code=code,
            message=message,
            retryable=retryable,
            details=details or {},
        ),
    )
