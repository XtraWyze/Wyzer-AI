"""Main-process owner for persistent coding sessions."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID

from wyzer.brain import ChatProvider
from wyzer.coding.agent import CodingAgent
from wyzer.coding.models import CodingAgentSettings, CodingSession, CodingSessionStatus
from wyzer.coding.workspace import CodingWorkspace, WorkspaceError
from wyzer.files.paths import common_user_folders
from wyzer.models import ChatMessage, StructuredError, ToolResult


class CodingAgentManager:
    """Retain coding state outside disposable tool workers."""

    PROXY_TOOLS = frozenset(
        {
            "coding_agent_start",
            "coding_agent_message",
            "coding_agent_status",
            "coding_agent_cancel",
        }
    )

    def __init__(
        self,
        provider: ChatProvider,
        settings: CodingAgentSettings | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings or CodingAgentSettings()
        self._sessions: dict[UUID, CodingSession] = {}
        self._workspaces: dict[UUID, CodingWorkspace] = {}
        self._provider_tasks: dict[UUID, asyncio.Task[Any]] = {}
        self._action_sessions: dict[UUID, UUID] = {}
        self._lock = RLock()
        self._operation_lock = asyncio.Lock()
        self._agent = CodingAgent(provider, self.settings, self._set_provider_task)

    @property
    def sessions(self) -> tuple[CodingSession, ...]:
        with self._lock:
            return tuple(
                session.model_copy(deep=True)
                for session in sorted(
                    self._sessions.values(), key=lambda item: item.updated_at, reverse=True
                )
            )

    async def execute_proxy(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        action_id: UUID,
        step_id: UUID,
    ) -> ToolResult:
        started = datetime.now(UTC)
        try:
            if tool_name == "coding_agent_start":
                data = await self.start(
                    arguments["workspace"],
                    arguments["task"],
                    action_id,
                    create_workspace=arguments.get("create_workspace", False),
                )
            elif tool_name == "coding_agent_message":
                data = await self.message(
                    arguments["message"], arguments.get("session_id"), action_id
                )
            elif tool_name == "coding_agent_status":
                data = self.status(arguments.get("session_id"))
            elif tool_name == "coding_agent_cancel":
                data = self.cancel(arguments.get("session_id"))
            else:
                raise CodingManagerError("UNKNOWN_TOOL", f"Unknown coding proxy: {tool_name}")
            evidence = (
                self._evidence(data)
                if tool_name
                in {"coding_agent_start", "coding_agent_message", "coding_agent_status"}
                else {}
            )
            return ToolResult(
                ok=True,
                tool=tool_name,
                action_id=action_id,
                step_id=step_id,
                started_at=started,
                finished_at=datetime.now(UTC),
                duration_ms=max(0, round((datetime.now(UTC) - started).total_seconds() * 1_000)),
                data=data,
                evidence=evidence,
            )
        except (CodingManagerError, WorkspaceError) as error:
            return ToolResult(
                ok=False,
                tool=tool_name,
                action_id=action_id,
                step_id=step_id,
                started_at=started,
                finished_at=datetime.now(UTC),
                duration_ms=max(0, round((datetime.now(UTC) - started).total_seconds() * 1_000)),
                error=StructuredError(
                    code=error.code,
                    message=str(error),
                    retryable=error.code not in {"INVALID_WORKSPACE", "PATH_OUTSIDE_WORKSPACE"},
                ),
            )
        except asyncio.CancelledError:
            return ToolResult(
                ok=False,
                tool=tool_name,
                action_id=action_id,
                step_id=step_id,
                started_at=started,
                finished_at=datetime.now(UTC),
                duration_ms=max(0, round((datetime.now(UTC) - started).total_seconds() * 1_000)),
                error=StructuredError(code="CODING_CANCELLED", message="Coding operation cancelled."),
            )
        except Exception as error:
            return ToolResult(
                ok=False,
                tool=tool_name,
                action_id=action_id,
                step_id=step_id,
                started_at=started,
                finished_at=datetime.now(UTC),
                duration_ms=max(0, round((datetime.now(UTC) - started).total_seconds() * 1_000)),
                error=StructuredError(
                    code="CODING_AGENT_FAILED",
                    message=str(error) or type(error).__name__,
                    retryable=True,
                ),
            )

    async def start(
        self,
        workspace: str,
        task: str,
        action_id: UUID,
        *,
        create_workspace: bool = False,
    ) -> dict[str, Any]:
        workspace_path = Path(workspace).expanduser()
        if not workspace_path.is_absolute():
            parts = workspace_path.parts
            known_folders = common_user_folders()
            grounded_root = known_folders.get(parts[0].casefold()) if parts else None
            if grounded_root is None:
                raise CodingManagerError(
                    "WORKSPACE_NOT_ABSOLUTE",
                    "Use an absolute workspace or a path rooted at a known user folder.",
                )
            workspace_path = Path(grounded_root).joinpath(*parts[1:])
        resolved_workspace = workspace_path.resolve(strict=False)
        if resolved_workspace.parent == resolved_workspace:
            raise CodingManagerError(
                "INVALID_WORKSPACE", "A filesystem root cannot be used as a coding workspace."
            )
        workspace_created = False
        if create_workspace and not workspace_path.exists():
            try:
                resolved_workspace.mkdir(parents=True, exist_ok=False)
            except OSError as error:
                raise CodingManagerError(
                    "WORKSPACE_CREATE_FAILED", f"Could not create workspace: {error}"
                ) from error
            workspace_path = resolved_workspace
            workspace_created = True
        coding_workspace = CodingWorkspace(
            workspace_path,
            command_timeout_seconds=self.settings.command_timeout_seconds,
            maximum_output_characters=self.settings.maximum_output_characters,
        )
        session = CodingSession(
            workspace=coding_workspace.root,
            current_task=task,
            last_task=task,
            messages=[ChatMessage(role="user", content=task)],
        )
        with self._lock:
            self._sessions[session.session_id] = session
            self._workspaces[session.session_id] = coding_workspace
            self._action_sessions[action_id] = session.session_id
        try:
            async with self._operation_lock:
                await self._agent.run(session, coding_workspace)
        except BaseException:
            session.status = CodingSessionStatus.FAILED
            session.current_task = None
            session.touch()
            raise
        finally:
            with self._lock:
                self._action_sessions.pop(action_id, None)
        session.current_task = None
        session.touch()
        return {**self._session_data(session), "workspace_created": workspace_created}

    async def message(
        self, message: str, session_id: str | None, action_id: UUID
    ) -> dict[str, Any]:
        session = self._select(session_id)
        if session.status == CodingSessionStatus.RUNNING:
            raise CodingManagerError("SESSION_BUSY", "The coding session is already running.")
        session.current_task = message
        session.last_task = message
        session.messages.append(ChatMessage(role="user", content=message))
        session.touch()
        with self._lock:
            self._action_sessions[action_id] = session.session_id
        try:
            async with self._operation_lock:
                await self._agent.run(session, self._workspaces[session.session_id])
        except BaseException:
            session.status = CodingSessionStatus.FAILED
            session.current_task = None
            session.touch()
            raise
        finally:
            with self._lock:
                self._action_sessions.pop(action_id, None)
        session.current_task = None
        session.touch()
        return self._session_data(session)

    def status(self, session_id: str | None = None) -> dict[str, Any]:
        return self._session_data(self._select(session_id))

    def cancel(self, session_id: str | None = None) -> dict[str, Any]:
        session = self._select(session_id)
        was_running = session.status == CodingSessionStatus.RUNNING
        session._cancelled = True
        task = self._provider_tasks.get(session.session_id)
        if task is not None:
            task.cancel()
        command_cancelled = self._workspaces[session.session_id].cancel_command()
        if was_running:
            session.status = CodingSessionStatus.CANCELLED
            session.current_task = None
            session.last_summary = "Coding operation was cancelled."
            session.touch()
        return {
            **self._session_data(session),
            "cancelled": was_running or command_cancelled,
        }

    def cancel_action(self, action_id: UUID) -> bool:
        with self._lock:
            session_id = self._action_sessions.get(action_id)
        if session_id is None:
            return False
        self.cancel(str(session_id))
        return True

    def model_context(self, maximum_characters: int = 2_000) -> dict[str, Any]:
        sessions = [self._session_data(session, compact=True) for session in self.sessions[:4]]
        payload = {
            "active_session_id": sessions[0]["session_id"] if sessions else None,
            "sessions": sessions,
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        while len(serialized) > maximum_characters and len(sessions) > 1:
            sessions.pop()
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        return payload if sessions else {}

    def _select(self, raw_session_id: str | None) -> CodingSession:
        with self._lock:
            if raw_session_id:
                try:
                    session_id = UUID(raw_session_id)
                except ValueError as error:
                    raise CodingManagerError("INVALID_SESSION_ID", "Invalid coding session ID.") from error
                session = self._sessions.get(session_id)
                if session is None:
                    raise CodingManagerError("UNKNOWN_SESSION", "Coding session was not found.")
                return session
            ordered = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
        if len(ordered) == 1:
            return ordered[0]
        if not ordered:
            raise CodingManagerError("NO_CODING_SESSION", "No coding session exists yet.")
        raise CodingManagerError(
            "AMBIGUOUS_CODING_SESSION", "Multiple coding sessions exist; provide session_id."
        )

    def _set_provider_task(
        self, session: CodingSession, task: asyncio.Task[Any] | None
    ) -> None:
        with self._lock:
            if task is None:
                self._provider_tasks.pop(session.session_id, None)
            else:
                self._provider_tasks[session.session_id] = task

    @staticmethod
    def _session_data(session: CodingSession, compact: bool = False) -> dict[str, Any]:
        verification = session.last_verification
        if compact and verification:
            verification = {
                "verification_status": verification.get("verification_status"),
                "successful_checks": verification.get("successful_checks", [])[-3:],
            }
        data: dict[str, Any] = {
            "session_id": str(session.session_id),
            "workspace": str(session.workspace),
            "status": session.status.value,
            "current_task": (
                session.current_task[:300]
                if compact and session.current_task
                else session.current_task
            ),
            "last_task": session.last_task[:300] if compact and session.last_task else session.last_task,
            "last_summary": (
                session.last_summary[:600]
                if compact and session.last_summary
                else session.last_summary
            ),
            "changed_files": session.changed_files[-20 if compact else -50 :],
            "last_verification": verification,
        }
        if not compact:
            data["commands_run"] = session.commands_run[-20:]
            data["created_at"] = session.created_at.isoformat()
            data["updated_at"] = session.updated_at.isoformat()
        return data

    @staticmethod
    def _evidence(data: dict[str, Any]) -> dict[str, Any]:
        verification = data.get("last_verification")
        status = (
            verification.get("verification_status", "unavailable")
            if isinstance(verification, dict)
            else "unavailable"
        )
        return {
            "verification_status": status,
            "predicate": "coding_task_completed_and_checked",
            "observed": {
                "changed_files": data.get("changed_files", []),
                "successful_checks": (
                    verification.get("successful_checks", [])
                    if isinstance(verification, dict)
                    else []
                ),
            },
        }


class CodingManagerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
