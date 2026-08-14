"""Bounded native-tool loop for one coding session."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from typing import Any

from wyzer.brain import ChatProvider
from wyzer.coding.models import CodingAgentSettings, CodingSession, CodingSessionStatus
from wyzer.coding.prompt import CODING_AGENT_SYSTEM_PROMPT
from wyzer.coding.tools import coding_native_tools, execute_coding_tool
from wyzer.coding.workspace import CodingWorkspace
from wyzer.models import ChatMessage, ChatRequestSettings


class CodingAgent:
    def __init__(
        self,
        provider: ChatProvider,
        settings: CodingAgentSettings,
        provider_task_changed: Callable[[CodingSession, asyncio.Task[Any] | None], None],
    ) -> None:
        self.provider = provider
        self.settings = settings
        self._provider_task_changed = provider_task_changed

    async def run(self, session: CodingSession, workspace: CodingWorkspace) -> None:
        session.status = CodingSessionStatus.RUNNING
        session._cancelled = False
        session.touch()
        empty_retry = False
        repeated: Counter[str] = Counter()
        for _round in range(self.settings.maximum_rounds):
            if session._cancelled:
                self._cancel(session)
                return
            messages = [
                ChatMessage(
                    role="system",
                    content=(
                        CODING_AGENT_SYSTEM_PROMPT
                        + f"\nAssigned workspace: {workspace.root}"
                    ),
                ),
                *session.messages,
            ]
            if empty_retry:
                messages.append(
                    ChatMessage(
                        role="system",
                        content="Return a concise final response or one valid native tool call now.",
                    )
                )
            task = asyncio.create_task(
                self.provider.chat(
                    messages,
                    coding_native_tools(),
                    ChatRequestSettings(max_output_tokens=self.settings.max_response_tokens),
                )
            )
            self._provider_task_changed(session, task)
            try:
                response = await task
            except asyncio.CancelledError:
                if session._cancelled:
                    self._cancel(session)
                    return
                raise
            finally:
                self._provider_task_changed(session, None)
            message = response.message
            if not message.tool_calls:
                content = (message.content or "").strip()
                if not content and not empty_retry:
                    empty_retry = True
                    continue
                if not content:
                    self._fail(session, "The coding model returned empty output twice.")
                    return
                session.messages.append(ChatMessage(role="assistant", content=content))
                session.last_summary = content
                session.status = CodingSessionStatus.IDLE
                self._sync_observations(session, workspace)
                self._bound_history(session)
                session.touch()
                return
            empty_retry = False
            if len(message.tool_calls) > 8:
                message = message.model_copy(update={"tool_calls": message.tool_calls[:8]})
            session.messages.append(message)
            for call in message.tool_calls:
                if session._cancelled:
                    self._cancel(session)
                    return
                signature = json.dumps(
                    [call.function.name, call.function.arguments],
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                repeated[signature] += 1
                if repeated[signature] > 3:
                    result = {
                        "ok": False,
                        "tool": call.function.name,
                        "error": {
                            "code": "REPEATED_TOOL_CALL",
                            "message": "The identical coding tool call repeated too many times.",
                        },
                    }
                else:
                    result = await execute_coding_tool(
                        workspace, call.function.name, call.function.arguments
                    )
                session.messages.append(
                    ChatMessage(
                        role="tool",
                        name=call.function.name,
                        tool_call_id=call.id,
                        content=self._bounded_result(result),
                    )
                )
                self._sync_observations(session, workspace)
            self._bound_history(session)
        self._fail(
            session,
            f"Coding agent stopped after {self.settings.maximum_rounds} rounds to avoid looping.",
        )
        self._sync_observations(session, workspace)

    def _bounded_result(self, result: dict[str, Any]) -> str:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
        limit = self.settings.tool_result_context_characters
        if len(serialized) <= limit:
            return serialized
        return json.dumps(
            {
                "ok": result.get("ok", False),
                "tool": result.get("tool"),
                "truncated": True,
                "summary": serialized[: max(1, limit - 100)],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _bound_history(self, session: CodingSession) -> None:
        limit = self.settings.maximum_history_messages
        if len(session.messages) > limit:
            start = len(session.messages) - limit
            if session.messages[start].role == "tool":
                for index in range(start - 1, -1, -1):
                    if session.messages[index].role == "assistant":
                        start = index
                        break
            session.messages[:] = session.messages[start:]

    @staticmethod
    def _sync_observations(session: CodingSession, workspace: CodingWorkspace) -> None:
        session.changed_files = sorted(workspace.changed_files)
        session.commands_run = workspace.commands_run[-20:]
        successful = [
            command
            for command in session.commands_run
            if command.get("exit_code") == 0
            and not command.get("timed_out")
            and CodingAgent._is_check_command(command.get("argv"))
        ]
        if successful:
            status = "verified"
        elif session.changed_files:
            status = "not_verified"
        else:
            status = "unavailable"
        session.last_verification = {
            "verification_status": status,
            "successful_checks": successful[-8:],
        }

    @staticmethod
    def _is_check_command(raw_argv: object) -> bool:
        if not isinstance(raw_argv, list) or not raw_argv:
            return False
        argv = [str(item).casefold() for item in raw_argv]
        executable = argv[0]
        if executable in {"pytest", "ruff", "mypy"}:
            return True
        if executable in {"pylint", "flake8", "pyright"}:
            return True
        if executable in {"cargo", "dotnet"}:
            return len(argv) > 1 and argv[1] in {"test", "check", "build"}
        if executable in {"npm", "pnpm", "yarn"}:
            return any(item in {"test", "check", "lint", "build"} for item in argv[1:3])
        if executable in {"python", "python3", "py"}:
            return len(argv) > 2 and argv[1] == "-m" and argv[2] in {
                "pytest",
                "ruff",
                "mypy",
                "compileall",
                "py_compile",
                "pylint",
                "flake8",
            }
        return False

    @staticmethod
    def _fail(session: CodingSession, message: str) -> None:
        session.status = CodingSessionStatus.FAILED
        session.last_summary = message
        session.current_task = None
        session.touch()

    @staticmethod
    def _cancel(session: CodingSession) -> None:
        session.status = CodingSessionStatus.CANCELLED
        session.last_summary = "Coding operation was cancelled."
        session.current_task = None
        session.touch()
