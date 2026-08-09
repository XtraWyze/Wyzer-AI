"""Bounded mutable conversation state owned by the main process."""

from __future__ import annotations

from contextlib import suppress
from typing import TypeVar

from wyzer.models import (
    AssistantResponse,
    ChatMessage,
    ConversationState,
    PendingConfirmation,
    ToolResult,
    UserRequest,
    WindowInfo,
)

ValueT = TypeVar("ValueT")


class ConversationManager:
    def __init__(self, limit: int = 40) -> None:
        if limit < 1:
            raise ValueError("conversation limit must be positive")
        self._limit = limit
        self._state = ConversationState()

    def record_user(self, request: UserRequest) -> None:
        self._append(self._state.recent_user_messages, request.text)
        self._append(
            self._state.recent_transcript,
            {"role": "user", "content": request.text},
        )
        self._state.current_user_goal = request.text
        self._append(self._state.model_messages, ChatMessage(role="user", content=request.text))

    def record_local_user(self, request: UserRequest) -> None:
        """Record a deterministic control reply without sending it back to the model."""
        self._append(self._state.recent_user_messages, request.text)
        self._append(self._state.recent_transcript, {"role": "user", "content": request.text})

    def record_response(self, response: AssistantResponse) -> None:
        self._append(self._state.recent_assistant_responses, response.text)
        self._append(
            self._state.recent_transcript,
            {"role": "assistant", "content": response.text},
        )
        self._append(
            self._state.model_messages,
            ChatMessage(role="assistant", content=response.text),
        )

    def record_local_response(self, response: AssistantResponse) -> None:
        self._append(self._state.recent_assistant_responses, response.text)
        self._append(
            self._state.recent_transcript,
            {"role": "assistant", "content": response.text},
        )

    def record_assistant_tool_calls(self, message: ChatMessage) -> None:
        self._append(self._state.model_messages, message)
        self._append(
            self._state.recent_transcript,
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [call.model_dump(mode="json") for call in message.tool_calls],
            },
        )

    def record_tool_result(
        self,
        result: ToolResult,
        model_content: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        self._append(self._state.recent_tool_results, result)
        self._append(
            self._state.recent_transcript,
            {
                "role": "tool",
                "tool": result.tool,
                "ok": result.ok,
                "data": result.data,
                "evidence": result.evidence,
                "warnings": result.warnings,
                "error": result.error.model_dump(mode="json") if result.error else None,
            },
        )
        if model_content is not None:
            self._append(
                self._state.model_messages,
                ChatMessage(
                    role="tool",
                    name=result.tool,
                    tool_call_id=tool_call_id,
                    content=model_content,
                ),
            )
        if result.tool in {
            "open_application",
            "open_file",
            "open_indexed_folder",
            "control_media",
            "move_named_window_to_monitor",
            "control_named_window",
            "list_open_windows",
            "is_process_running",
            "get_foreground_window",
            "browser_start",
            "browser_open_url",
            "browser_search_web",
            "browser_inspect_page",
            "browser_click",
            "browser_type_text",
            "browser_history",
            "browser_switch_tab",
            "browser_close_tab",
            "list_directory",
            "create_directory",
            "copy_path",
            "move_path",
            "rename_path",
            "delete_path",
        }:
            data = result.data or {}
            observed = result.evidence.get("observed", {})
            # Prefer stable application or query identities over transient Win32 titles.
            target = (
                data.get("application")
                or data.get("target")
                or data.get("query")
                or data.get("name")
                or data.get("url")
                or data.get("destination")
                or data.get("path")
                or data.get("source")
            )
            has_stable_target = isinstance(target, str) and bool(target.strip())
            window = data.get("window")
            if isinstance(window, dict):
                if not has_stable_target:
                    target = window.get("application") or window.get("title") or target
                with suppress(ValueError):
                    self.mention_window(WindowInfo.model_validate(window))
            raw_windows = data.get("windows")
            if (
                isinstance(raw_windows, list)
                and len(raw_windows) == 1
                and isinstance(raw_windows[0], dict)
            ):
                with suppress(ValueError):
                    observed_window = WindowInfo.model_validate(raw_windows[0])
                    self.mention_window(observed_window)
                    if not has_stable_target:
                        target = observed_window.application or observed_window.title or target
            if not target and isinstance(observed, dict):
                target = observed.get("title")
            if not target and result.error is not None:
                target = (
                    result.error.details.get("application")
                    or result.error.details.get("path")
                    or result.error.details.get("query")
                )
            self._state.last_action = {
                "tool": result.tool,
                "ok": result.ok,
                "target": target,
                "data": data,
                "evidence": result.evidence,
                "error": result.error.model_dump(mode="json") if result.error else None,
            }
            if result.tool in {
                "open_application",
                "list_open_windows",
                "is_process_running",
            } and isinstance(target, str):
                self.mention_application(target)
            if result.tool == "open_file" and isinstance(target, str):
                self._append(self._state.recently_mentioned_files, target)
            if result.tool in {
                "list_directory",
                "create_directory",
                "copy_path",
                "move_path",
                "rename_path",
                "delete_path",
            }:
                file_target = (
                    data.get("destination") or data.get("path") or data.get("source") or target
                )
                if isinstance(file_target, str) and file_target:
                    self._append(self._state.recently_mentioned_files, file_target)
            if result.tool.startswith("browser_"):
                url = data.get("url") or data.get("active_url")
                if isinstance(url, str) and url:
                    self._append(self._state.recently_mentioned_websites, url)
        if result.tool in {
            "control_master_audio",
            "control_application_audio",
            "mute_all_audio_except",
        }:
            data = result.data or {}
            self._append(
                self._state.recent_audio_targets,
                {
                    "tool": result.tool,
                    "target": data.get("target") or data.get("kept_applications"),
                    "operation": data.get("operation"),
                    "session_ids": data.get("session_ids", []),
                },
            )

    def set_pending_offer(self, offer: str | None) -> None:
        self._state.pending_offer = offer

    def set_pending_confirmation(self, confirmation: PendingConfirmation | None) -> None:
        self._state.pending_confirmations.clear()
        if confirmation is not None:
            self._state.pending_confirmations.append(confirmation)

    def confirm_last_action(self, confirmed: bool) -> None:
        if self._state.last_action is not None:
            self._state.last_action["user_confirmed"] = confirmed

    def clear_pending_context(self) -> None:
        self._state.pending_offer = None
        self._state.last_action = None

    def set_active_task(self, task: str | None) -> None:
        self._state.active_task = task

    def complete_task(self, task: str) -> None:
        self._append(self._state.completed_tasks, task)
        self._state.active_task = None

    def cancel_task(self, task: str) -> None:
        self._append(self._state.cancelled_tasks, task)
        self._state.active_task = None

    def record_correction(self, text: str) -> None:
        self._append(self._state.recent_user_corrections, text)

    def record_question(self, text: str) -> None:
        self._append(self._state.unresolved_questions, text)

    def mention_application(self, name: str) -> None:
        self._append(self._state.recently_mentioned_applications, name)

    def mention_window(self, window: WindowInfo) -> None:
        self._append(self._state.recently_referenced_windows, window)

    def set_remembered_facts(self, facts: list[str]) -> None:
        self._state.remembered_facts = list(facts[:100])

    def snapshot(self) -> ConversationState:
        return self._state.model_copy(deep=True)

    def _append(self, values: list[ValueT], value: ValueT) -> None:
        values.append(value)
        del values[: -self._limit]
