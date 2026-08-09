"""Main-process-only world state updated from typed deterministic observations."""

from __future__ import annotations

import re
from collections import deque
from datetime import UTC, datetime
from threading import RLock

from wyzer.models import (
    BrowserScene,
    DesktopPerception,
    DesktopScene,
    PendingConfirmation,
    SceneBrowserTab,
    SceneChange,
    SceneElement,
    SceneSource,
    StructuredError,
    ToolResult,
    VerificationResult,
    WindowInfo,
    WorldStateSnapshot,
)

_SENSITIVE_VISIBLE_TEXT = re.compile(
    r"\b(?:password|passcode|pin|api[ -]?key|access[ -]?token|secret|private key|"
    r"credit card|debit card|bank account|routing number|social security|ssn)\b",
    re.I,
)


def _window_matches_query(window: WindowInfo, query: str) -> bool:
    compact_query = "".join(character for character in query.casefold() if character.isalnum())
    compact_query = {"fileexplorer": "explorer"}.get(compact_query, compact_query)
    observed = "".join(
        character
        for character in f"{window.title} {window.application or ''}".casefold()
        if character.isalnum()
    )
    return bool(compact_query and compact_query in observed)


class WorldStateManager:
    def __init__(self, history_limit: int = 100) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._lock = RLock()
        self._revision = 0
        self._foreground_window: WindowInfo | None = None
        self._known_open_windows: tuple[WindowInfo, ...] = ()
        self._monitor_layout: tuple[dict[str, object], ...] = ()
        self._focus_history: deque[WindowInfo] = deque(maxlen=history_limit)
        self._last_perception: DesktopPerception | None = None
        self._desktop_scene = DesktopScene()
        self._active_task: str | None = None
        self._pending_confirmation: PendingConfirmation | None = None
        self._recent_tool_calls: deque[ToolResult] = deque(maxlen=history_limit)
        self._recent_errors: deque[StructuredError] = deque(maxlen=history_limit)
        self._recent_verifications: deque[VerificationResult] = deque(maxlen=history_limit)
        self._operating_mode = "text"

    def apply_perception(self, perception: DesktopPerception) -> None:
        with self._lock:
            old_foreground = self._foreground_window
            self._last_perception = perception
            self._foreground_window = perception.foreground_window
            if perception.foreground_window is not None:
                by_handle = {window.handle: window for window in self._known_open_windows}
                by_handle[perception.foreground_window.handle] = perception.foreground_window
                self._known_open_windows = tuple(by_handle.values())
                if old_foreground != perception.foreground_window:
                    self._focus_history.append(perception.foreground_window)
            self._refresh_scene_from_perception(perception)
            self._revision += 1

    def replace_windows(self, windows: list[WindowInfo]) -> None:
        with self._lock:
            self._known_open_windows = tuple(windows)
            self._refresh_scene_windows("windows", fresh_for_seconds=10.0)
            self._revision += 1

    def replace_monitors(self, monitors: list[dict[str, object]]) -> None:
        with self._lock:
            self._monitor_layout = tuple(dict(monitor) for monitor in monitors)
            self._revision += 1

    def record_tool_result(self, result: ToolResult) -> None:
        with self._lock:
            self._recent_tool_calls.append(result)
            if result.error is not None:
                self._recent_errors.append(result.error)
            self._revision += 1

    def apply_tool_observation(self, result: ToolResult) -> None:
        """Update desktop facts only from successful, typed deterministic tool output."""
        if not result.ok or result.data is None:
            return
        with self._lock:
            if result.tool == "get_foreground_window":
                raw_window = result.data.get("window")
                self._foreground_window = (
                    WindowInfo.model_validate(raw_window) if raw_window is not None else None
                )
                if self._foreground_window is not None:
                    self._focus_history.append(self._foreground_window)
                self._revision += 1
            elif result.tool == "list_open_windows":
                raw_windows = result.data.get("windows", [])
                observed = [WindowInfo.model_validate(window) for window in raw_windows]
                raw_monitor = result.data.get("monitor")
                query = result.data.get("query")
                monitor_id = (
                    str(raw_monitor["monitor_id"])
                    if isinstance(raw_monitor, dict)
                    and isinstance(raw_monitor.get("monitor_id"), str)
                    else None
                )
                if isinstance(query, str) and query:
                    by_handle = {
                        window.handle: window
                        for window in self._known_open_windows
                        if not (
                            _window_matches_query(window, query)
                            and (monitor_id is None or window.monitor_id == monitor_id)
                        )
                    }
                    by_handle.update({window.handle: window for window in observed})
                    self._known_open_windows = tuple(by_handle.values())
                elif monitor_id is not None:
                    by_handle = {
                        window.handle: window
                        for window in self._known_open_windows
                        if window.monitor_id != monitor_id
                    }
                    by_handle.update({window.handle: window for window in observed})
                    self._known_open_windows = tuple(by_handle.values())
                else:
                    self._known_open_windows = tuple(observed)
                self._revision += 1
            elif result.tool == "is_process_running":
                query = result.data.get("name")
                raw_windows = result.data.get("windows", [])
                if isinstance(query, str) and query:
                    observed = [WindowInfo.model_validate(window) for window in raw_windows]
                    by_handle = {
                        window.handle: window
                        for window in self._known_open_windows
                        if not _window_matches_query(window, query)
                    }
                    by_handle.update({window.handle: window for window in observed})
                    self._known_open_windows = tuple(by_handle.values())
                    self._revision += 1
            elif result.tool == "get_monitor_layout":
                raw_monitors = result.data.get("monitors", [])
                self._monitor_layout = tuple(dict(monitor) for monitor in raw_monitors)
                self._revision += 1
            elif result.evidence.get("verification_status") == "verified":
                self._apply_verified_action(result)
            self._apply_scene_observation(result)

    def _apply_scene_observation(self, result: ToolResult) -> None:
        """Merge successful tool observations into one privacy-aware desktop scene.

        Callers hold ``self._lock``. The scene is evidence only: it never selects an action.
        """
        assert result.data is not None
        data = result.data
        previous = self._desktop_scene
        browser = previous.browser
        visual_summary = previous.visual_summary
        visible_text = previous.visible_text
        elements = previous.elements
        dialogs = previous.dialogs
        redacted = previous.redacted_content
        source_name: str | None = None
        freshness = 30.0
        confidence = 1.0
        changes: list[SceneChange] = []

        if result.tool in {"get_foreground_window", "list_open_windows", "is_process_running"}:
            source_name = "windows"
            freshness = 10.0
        elif result.tool == "inspect_screen":
            source_name = "vision"
            freshness = 20.0
            confidence = 0.8
            visual_summary = self._safe_text(str(data.get("summary") or "")) or None
            raw_text = data.get("visible_text", [])
            visible_text, removed = self._safe_text_items(raw_text, 40)
            redacted = redacted or removed
            raw_elements = data.get("relevant_elements", [])
            elements = self._scene_elements(raw_elements, "vision", 50)
            title = str(data.get("window_title") or "").strip()
            if title:
                dialogs = [title] if "dialog" in title.casefold() else dialogs
        elif result.tool == "inspect_desktop_ui":
            source_name = "ui_automation"
            freshness = 15.0
            title = str(data.get("window_title") or "").strip()
            raw_elements = data.get("elements", [])
            elements = self._scene_elements(raw_elements, "ui_automation", 80)
            if title and "dialog" in title.casefold():
                dialogs = [title]
        elif result.tool.startswith("browser_"):
            source_name = (
                "browser_page" if result.tool == "browser_inspect_page" else "browser_navigation"
            )
            freshness = 15.0
            browser = self._scene_browser(browser, result.tool, data)
            if result.tool == "browser_inspect_page":
                raw_text = str(data.get("text") or "").splitlines()
                visible_text, removed = self._safe_text_items(raw_text, 40)
                redacted = redacted or removed
                elements = self._scene_elements(data.get("elements", []), "browser", 80)
            elif browser.active_url != (previous.browser.active_url if previous.browser else None):
                visible_text = []
                elements = [item for item in elements if item.source != "browser"]

        if previous.foreground_window != self._foreground_window:
            label = (
                self._foreground_window.title
                if self._foreground_window is not None
                else "no window"
            )
            changes.append(SceneChange(kind="foreground_changed", summary=f"Foreground: {label}"))
        old_url = previous.browser.active_url if previous.browser is not None else None
        new_url = browser.active_url if browser is not None else None
        if old_url != new_url and new_url:
            changes.append(SceneChange(kind="browser_navigated", summary=f"Browser: {new_url}"))
        if previous.visual_summary != visual_summary and visual_summary:
            changes.append(SceneChange(kind="visual_state_changed", summary=visual_summary[:500]))

        sources = list(previous.sources)
        if source_name is not None:
            sources = [source for source in sources if source.name != source_name]
            sources.append(
                SceneSource(
                    name=source_name,
                    fresh_for_seconds=freshness,
                    confidence=confidence,
                )
            )
        merged_changes = [*previous.recent_changes, *changes][-20:]
        self._desktop_scene = DesktopScene(
            foreground_window=self._foreground_window,
            windows=list(self._known_open_windows),
            browser=browser,
            visual_summary=visual_summary,
            visible_text=visible_text,
            elements=elements,
            dialogs=dialogs,
            redacted_content=redacted,
            sources=sources[-20:],
            recent_changes=merged_changes,
            captured_at=datetime.now(UTC),
        )

    def _refresh_scene_from_perception(self, perception: DesktopPerception) -> None:
        visible_text, redacted = self._safe_text_items(perception.visible_text, 40)
        dialogs, dialog_redacted = self._safe_text_items(
            [
                str(item.get("name") or item.get("title") or "")
                for item in perception.dialogs
                if isinstance(item, dict)
            ],
            20,
        )
        source = "desktop_perception"
        sources = [item for item in self._desktop_scene.sources if item.name != source]
        sources.append(SceneSource(name=source, fresh_for_seconds=15.0, confidence=1.0))
        self._desktop_scene = self._desktop_scene.model_copy(
            update={
                "captured_at": datetime.now(UTC),
                "foreground_window": self._foreground_window,
                "windows": list(self._known_open_windows),
                "visible_text": visible_text or self._desktop_scene.visible_text,
                "elements": self._scene_elements(perception.controls, source, 80)
                or self._desktop_scene.elements,
                "dialogs": dialogs or self._desktop_scene.dialogs,
                "redacted_content": self._desktop_scene.redacted_content
                or redacted
                or dialog_redacted,
                "sources": sources[-20:],
            }
        )

    def _refresh_scene_windows(self, source: str, *, fresh_for_seconds: float) -> None:
        sources = [item for item in self._desktop_scene.sources if item.name != source]
        sources.append(SceneSource(name=source, fresh_for_seconds=fresh_for_seconds))
        self._desktop_scene = self._desktop_scene.model_copy(
            update={
                "captured_at": datetime.now(UTC),
                "foreground_window": self._foreground_window,
                "windows": list(self._known_open_windows),
                "sources": sources[-20:],
            }
        )

    @staticmethod
    def _safe_text(value: str) -> str:
        compact = " ".join(value.split())
        return (
            "[sensitive text hidden]"
            if _SENSITIVE_VISIBLE_TEXT.search(compact)
            else compact[:1_000]
        )

    @classmethod
    def _safe_text_items(cls, raw: object, limit: int) -> tuple[list[str], bool]:
        if not isinstance(raw, list):
            return [], False
        redacted = False
        cleaned: list[str] = []
        for item in raw:
            text = " ".join(str(item).split())
            if not text:
                continue
            hidden = _SENSITIVE_VISIBLE_TEXT.search(text) is not None
            redacted = redacted or hidden
            cleaned.append("[sensitive text hidden]" if hidden else text[:1_000])
            if len(cleaned) >= limit:
                break
        return cleaned, redacted

    @classmethod
    def _scene_elements(cls, raw: object, source: str, limit: int) -> list[SceneElement]:
        if not isinstance(raw, list):
            return []
        rows: list[SceneElement] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = cls._safe_text(str(item.get("label") or item.get("name") or ""))
            kind = str(item.get("kind") or item.get("control_type") or item.get("role") or "")
            if not label or not kind:
                continue
            state = item.get("state")
            rows.append(
                SceneElement(
                    label=label,
                    kind=kind[:100],
                    state=str(state)[:200] if state is not None else None,
                    source=source,
                )
            )
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _scene_browser(
        current: BrowserScene | None,
        tool: str,
        data: dict[str, object],
    ) -> BrowserScene:
        if tool == "browser_stop":
            return BrowserScene(running=False)
        running = bool(data.get("running", True))
        title = str(data.get("title") or (current.title if current else "") or "") or None
        active_url = (
            str(
                data.get("url")
                or data.get("active_url")
                or (current.active_url if current else "")
                or ""
            )
            or None
        )
        tabs = list(current.tabs) if current is not None else []
        raw_tabs = data.get("tabs")
        if isinstance(raw_tabs, list):
            tabs = []
            for item in raw_tabs[:50]:
                if not isinstance(item, dict):
                    continue
                try:
                    tabs.append(SceneBrowserTab.model_validate(item))
                except ValueError:
                    continue
        return BrowserScene(running=running, title=title, active_url=active_url, tabs=tabs)

    def _apply_verified_action(self, result: ToolResult) -> None:
        if result.data is None:
            return
        if (
            result.tool == "control_named_window"
            and result.evidence.get("predicate") == "named_window_close"
        ):
            raw_handles = result.data.get("window_handles", [])
            handles = {
                int(handle) for handle in raw_handles if isinstance(handle, int) and handle > 0
            }
            handle = result.data.get("window_handle")
            if isinstance(handle, int) and handle > 0:
                handles.add(handle)
            self._known_open_windows = tuple(
                window for window in self._known_open_windows if window.handle not in handles
            )
            if self._foreground_window and self._foreground_window.handle in handles:
                self._foreground_window = None
            self._revision += 1
            return
        raw_window = result.data.get("window")
        if raw_window is None:
            return
        window = WindowInfo.model_validate(raw_window)
        by_handle = {item.handle: item for item in self._known_open_windows}
        by_handle[window.handle] = window
        self._known_open_windows = tuple(by_handle.values())
        if result.tool == "open_application" or (
            result.tool == "control_named_window"
            and result.evidence.get("predicate") == "named_window_focus"
        ):
            self._foreground_window = window
            self._focus_history.append(window)
        self._revision += 1

    def record_verification(self, result: VerificationResult) -> None:
        with self._lock:
            self._recent_verifications.append(result)
            self._revision += 1

    def set_task(self, task: str | None) -> None:
        with self._lock:
            self._active_task = task
            self._revision += 1

    def set_confirmation(self, confirmation: PendingConfirmation | None) -> None:
        with self._lock:
            self._pending_confirmation = confirmation
            self._revision += 1

    def set_operating_mode(self, mode: str) -> None:
        if mode not in {"text", "voice"}:
            raise ValueError("operating mode must be text or voice")
        with self._lock:
            self._operating_mode = mode
            self._revision += 1

    def snapshot(self) -> WorldStateSnapshot:
        with self._lock:
            return WorldStateSnapshot(
                revision=self._revision,
                foreground_window=self._foreground_window,
                known_open_windows=list(self._known_open_windows),
                monitor_layout=[dict(item) for item in self._monitor_layout],
                focus_history=list(self._focus_history),
                last_desktop_perception=self._last_perception,
                desktop_scene=self._desktop_scene,
                active_task=self._active_task,
                pending_confirmation=self._pending_confirmation,
                recent_tool_calls=list(self._recent_tool_calls),
                recent_errors=list(self._recent_errors),
                recent_verification_results=list(self._recent_verifications),
                operating_mode=self._operating_mode,
            )
