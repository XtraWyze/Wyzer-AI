"""Compact, session-only grounding derived from authoritative tool results.

This module records facts. It deliberately does not inspect user text, resolve
references, select tools, or trigger actions; those decisions remain with the LLM.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from wyzer.models import ToolResult, WorldStateSnapshot

logger = logging.getLogger(__name__)

EntityKind = Literal["application", "window", "file", "folder", "project", "page", "tab"]


class SessionEntity(BaseModel):
    """A recently observed entity with only authoritative, useful identity fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntityKind
    name: str | None = None
    application: str | None = None
    title: str | None = None
    handle: int | None = None
    monitor: str | None = None
    path: str | None = None
    url: str | None = None
    tab_index: int | None = Field(default=None, ge=1)

    @property
    def identity(self) -> tuple[str, object]:
        if self.handle is not None:
            return self.kind, self.handle
        if self.path:
            return self.kind, self.path.casefold()
        if self.url:
            return self.kind, (self.tab_index, self.url)
        return self.kind, (self.name or self.application or self.title or "").casefold()


class SessionMonitor(BaseModel):
    """A monitor using Wyzer's user-facing number/label plus its backend identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    monitor_id: str | None = None
    number: int | None = Field(default=None, ge=1)
    label: str | None = None


class SessionAction(BaseModel):
    """One bounded factual tool outcome, not an inferred user intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    ok: bool
    target: str | None = None
    error: str | None = None


class SessionContext(BaseModel):
    """Detached snapshot of short-lived task continuity state."""

    model_config = ConfigDict(extra="forbid")

    active_app: str | None = None
    active_window: SessionEntity | None = None
    previous_app: str | None = None
    previous_window: SessionEntity | None = None
    current_project: str | None = None
    current_folder: str | None = None
    last_file: SessionEntity | None = None
    recent_files: list[SessionEntity] = Field(default_factory=list)
    last_browser_page: SessionEntity | None = None
    last_browser_tab: SessionEntity | None = None
    last_monitor: SessionMonitor | None = None
    previous_monitor: SessionMonitor | None = None
    last_tool_call: dict[str, Any] | None = None
    last_tool_result: dict[str, Any] | None = None
    recent_entities: list[SessionEntity] = Field(default_factory=list)
    recent_actions: list[SessionAction] = Field(default_factory=list)


class SessionContextManager:
    """Maintain compact session facts from completed tool calls only."""

    _WINDOW_TOOLS: ClassVar[frozenset[str]] = frozenset({
        "open_application",
        "open_file",
        "open_indexed_folder",
        "control_named_window",
        "move_named_window_to_monitor",
        "get_foreground_window",
        "list_open_windows",
        "is_process_running",
    })
    _FILE_TOOLS: ClassVar[frozenset[str]] = frozenset({
        "search_files",
        "read_text_file",
        "write_text_file",
        "edit_text_file",
        "append_text_file",
        "list_directory",
        "open_file",
        "open_indexed_folder",
        "create_directory",
        "copy_path",
        "move_path",
        "rename_path",
        "delete_path",
    })
    _SAFE_ARGUMENT_KEYS: ClassVar[frozenset[str]] = frozenset({
        "action",
        "application",
        "destination",
        "index",
        "monitor",
        "name",
        "new_tab",
        "path",
        "query",
        "source",
        "target",
        "url",
        "window",
    })

    def __init__(self, history_limit: int = 8) -> None:
        if history_limit < 1:
            raise ValueError("session context history limit must be positive")
        self._history_limit = history_limit
        self._state = SessionContext()
        self._lock = RLock()

    def record_tool_result(
        self,
        result: ToolResult,
        arguments: dict[str, Any] | None = None,
        *,
        before: WorldStateSnapshot | None = None,
        after: WorldStateSnapshot | None = None,
    ) -> None:
        """Record one completed call and update entities only for observed success."""
        with self._lock:
            target = self._target(result, arguments)
            succeeded = self._effect_succeeded(result)
            self._state.last_tool_call = {
                "tool": result.tool,
                **self._compact_arguments(arguments or {}),
            }
            self._state.last_tool_result = {
                "tool": result.tool,
                "ok": succeeded,
                **({"target": target} if target else {}),
                **(
                    {"error": result.error.code}
                    if result.error is not None
                    else {}
                ),
            }
            if not self._is_coordination_tool(result.tool):
                self._append_action(
                    SessionAction(
                        tool=result.tool,
                        ok=succeeded,
                        target=target,
                        error=result.error.code if result.error is not None else None,
                    )
                )
            if succeeded:
                self._update_success(result, arguments or {}, before=before, after=after)
            logger.debug("[session-context] %s", self.debug_summary())

    def snapshot(self) -> SessionContext:
        with self._lock:
            return self._state.model_copy(deep=True)

    def model_context(self, maximum_characters: int = 2_400) -> dict[str, Any]:
        """Return a token-conscious model snapshot, bounded independently of raw results."""
        if maximum_characters < 512:
            raise ValueError("session model context limit must be at least 512 characters")
        state = self.snapshot()
        context: dict[str, Any] = {
            "active_window": self._entity_context(state.active_window),
            "previous_window": self._entity_context(state.previous_window),
            "current_project": state.current_project,
            "current_folder": state.current_folder,
            "last_file": self._entity_context(state.last_file),
            "browser_page": self._entity_context(state.last_browser_page),
            "browser_tab": self._entity_context(state.last_browser_tab),
            "last_monitor": self._monitor_context(state.last_monitor),
            "previous_monitor": self._monitor_context(state.previous_monitor),
            "last_tool": state.last_tool_result,
            "recent_actions": [
                action.model_dump(exclude_none=True) for action in state.recent_actions
            ],
            "recent_entities": [
                self._entity_context(entity) for entity in state.recent_entities
            ],
        }
        context = {key: value for key, value in context.items() if value not in (None, [], {})}
        while self._serialized_size(context) > maximum_characters:
            actions = context.get("recent_actions")
            entities = context.get("recent_entities")
            if isinstance(actions, list) and len(actions) > 3:
                actions.pop(0)
                continue
            if isinstance(entities, list) and len(entities) > 3:
                entities.pop(0)
                continue
            if "last_tool" in context:
                context.pop("last_tool")
                continue
            break
        return context

    def debug_summary(self) -> str:
        state = self.snapshot()
        active = state.active_window
        previous = state.previous_window
        return " ".join(
            (
                f"active_window={self._entity_label(active)}",
                f"previous_window={self._entity_label(previous)}",
                f"last_file={state.last_file.path if state.last_file else None}",
                f"last_monitor={state.last_monitor.label if state.last_monitor else None}",
                f"recent_entities={len(state.recent_entities)}",
                f"recent_actions={len(state.recent_actions)}",
            )
        )

    def _update_success(
        self,
        result: ToolResult,
        arguments: dict[str, Any],
        *,
        before: WorldStateSnapshot | None,
        after: WorldStateSnapshot | None,
    ) -> None:
        data = result.data or {}
        if result.tool in self._WINDOW_TOOLS:
            self._update_windows(result, arguments, data, before=before, after=after)
        if result.tool in self._FILE_TOOLS:
            self._update_files(result.tool, data)
        if result.tool.startswith("browser_"):
            self._update_browser(result.tool, data, arguments)

    def _update_windows(
        self,
        result: ToolResult,
        arguments: dict[str, Any],
        data: dict[str, Any],
        *,
        before: WorldStateSnapshot | None,
        after: WorldStateSnapshot | None,
    ) -> None:
        raw_window = data.get("window")
        entity = self._window_entity(raw_window, result.tool, data, after)
        if entity is None:
            raw_windows = data.get("windows")
            if isinstance(raw_windows, list) and len(raw_windows) == 1:
                entity = self._window_entity(raw_windows[0], result.tool, data, after)
        if entity is None and result.tool == "control_named_window":
            handle = data.get("window_handle")
            entity = self._known_window(handle, data.get("target") or arguments.get("window"))

        action = str(arguments.get("action") or "").casefold()
        if result.tool == "control_named_window" and action == "close":
            if entity is not None:
                self._append_entity(entity)
                if self._state.active_window is not None and (
                    self._state.active_window.identity == entity.identity
                ):
                    self._state.active_window = self._state.previous_window
                    self._state.active_app = self._state.previous_app
                    self._state.previous_window = None
                    self._state.previous_app = None
            return

        make_active = result.tool in {
            "open_application",
            "open_file",
            "open_indexed_folder",
            "control_named_window",
            "move_named_window_to_monitor",
            "get_foreground_window",
        }
        if entity is not None:
            current = self._state.active_window
            if (
                current is not None
                and current.identity == entity.identity
                and current.name
            ):
                # Keep the model-authored concrete name that successfully opened/focused the
                # same authoritative handle instead of replacing it with an executable name.
                entity = entity.model_copy(update={"name": current.name})
            self._append_entity(entity)
            if make_active:
                self._set_active_window(entity)

        if result.tool == "move_named_window_to_monitor":
            source = self._monitor_from(data.get("source_monitor"), before)
            target = self._monitor_from(
                data.get("observed_monitor") or data.get("target_monitor"), after
            )
            if source is not None and target is not None:
                self._state.previous_monitor = source
                self._state.last_monitor = target
        elif entity is not None:
            monitor = self._monitor_from_id(
                self._raw_monitor_id(raw_window), after or before
            )
            if monitor is not None:
                self._state.last_monitor = monitor

    def _update_files(self, tool: str, data: dict[str, Any]) -> None:
        if tool == "search_files":
            matches = data.get("matches")
            if isinstance(matches, list):
                for match in matches[:5]:
                    if isinstance(match, dict):
                        self._record_file_path(match.get("path"), make_last=False)
            return

        if tool == "open_indexed_folder":
            path = self._path(data.get("target"))
            if path:
                self._state.current_project = path
                self._state.current_folder = path
                self._append_entity(SessionEntity(kind="project", name=Path(path).name, path=path))
            return

        if tool == "open_file":
            target = self._path(data.get("target"))
            if not target:
                return
            if data.get("target_kind") == "folder":
                self._state.current_folder = target
                self._append_entity(SessionEntity(kind="folder", name=Path(target).name, path=target))
            else:
                self._record_file_path(target, make_last=True)
            return

        if tool == "read_text_file":
            self._record_file_path(data.get("path"), make_last=True)
            return

        if tool in {"write_text_file", "edit_text_file", "append_text_file"}:
            self._record_file_path(data.get("path"), make_last=True)
            return

        if tool == "list_directory":
            path = self._path(data.get("path"))
            if path:
                self._state.current_folder = path
                self._append_entity(SessionEntity(kind="folder", name=Path(path).name, path=path))
            entries = data.get("entries")
            if isinstance(entries, list):
                for entry in entries[:5]:
                    if not isinstance(entry, dict):
                        continue
                    entry_path = self._path(entry.get("path"))
                    if not entry_path:
                        continue
                    if entry.get("kind") == "folder":
                        entity = SessionEntity(
                            kind="folder", name=Path(entry_path).name, path=entry_path
                        )
                    else:
                        entity = SessionEntity(
                            kind="file", name=Path(entry_path).name, path=entry_path
                        )
                    self._append_entity(entity)
            return

        path = data.get("destination") or data.get("path") or data.get("source")
        kind = data.get("kind")
        if kind == "folder":
            resolved = self._path(path)
            if resolved:
                self._append_entity(
                    SessionEntity(kind="folder", name=Path(resolved).name, path=resolved)
                )
        elif kind == "file":
            self._record_file_path(path, make_last=False)

    def _update_browser(
        self, tool: str, data: dict[str, Any], arguments: dict[str, Any]
    ) -> None:
        if tool == "browser_stop":
            self._state.last_browser_page = None
            self._state.last_browser_tab = None
            return
        tabs = data.get("tabs")
        if isinstance(tabs, list):
            active = next(
                (tab for tab in tabs if isinstance(tab, dict) and tab.get("active")),
                None,
            )
            if isinstance(active, dict):
                entity = self._page_entity(active, tab_index=active.get("index"))
                if entity is not None:
                    self._state.last_browser_page = entity
                    self._state.last_browser_tab = entity.model_copy(update={"kind": "tab"})
                    self._append_entity(self._state.last_browser_tab)
            return
        index = data.get("index") or arguments.get("index")
        entity = self._page_entity(data, tab_index=index)
        if entity is not None:
            self._state.last_browser_page = entity
            if isinstance(index, int) and index >= 1:
                self._state.last_browser_tab = entity.model_copy(
                    update={"kind": "tab", "tab_index": index}
                )
                self._append_entity(self._state.last_browser_tab)
            else:
                self._append_entity(entity)

    def _set_active_window(self, entity: SessionEntity) -> None:
        current = self._state.active_window
        application = entity.name or entity.application
        if current is not None and current.identity != entity.identity:
            self._state.previous_window = current
            self._state.previous_app = self._state.active_app
        self._state.active_window = entity
        self._state.active_app = application

    def _window_entity(
        self,
        raw: object,
        tool: str,
        data: dict[str, Any],
        world: WorldStateSnapshot | None,
    ) -> SessionEntity | None:
        if not isinstance(raw, dict):
            return None
        handle = raw.get("handle")
        if not isinstance(handle, int) or handle <= 0:
            return None
        stable_name = None
        if tool == "open_application":
            stable_name = data.get("application")
        elif tool in {"control_named_window", "move_named_window_to_monitor"}:
            stable_name = data.get("target")
        monitor = self._monitor_from_id(self._raw_monitor_id(raw), world)
        return SessionEntity(
            kind="window",
            name=self._text(stable_name, 200),
            application=self._text(raw.get("application"), 200),
            title=self._text(raw.get("title"), 300),
            handle=handle,
            monitor=monitor.label if monitor is not None else None,
        )

    def _known_window(self, raw_handle: object, raw_name: object) -> SessionEntity | None:
        if isinstance(raw_handle, int):
            for entity in reversed(self._state.recent_entities):
                if entity.kind == "window" and entity.handle == raw_handle:
                    return entity
        name = self._text(raw_name, 200)
        if name:
            for entity in reversed(self._state.recent_entities):
                if entity.kind != "window":
                    continue
                values = {entity.name, entity.application, entity.title}
                if any(value and name.casefold() in value.casefold() for value in values):
                    return entity
        return None

    def _record_file_path(self, raw_path: object, *, make_last: bool) -> None:
        path = self._path(raw_path)
        if not path:
            return
        entity = SessionEntity(kind="file", name=Path(path).name, path=path)
        self._append_bounded(self._state.recent_files, entity, deduplicate=True)
        self._append_entity(entity)
        if make_last:
            self._state.last_file = entity
            self._state.current_folder = str(Path(path).parent)

    def _append_entity(self, entity: SessionEntity) -> None:
        self._append_bounded(self._state.recent_entities, entity, deduplicate=True)

    def _append_action(self, action: SessionAction) -> None:
        self._append_bounded(self._state.recent_actions, action, deduplicate=False)

    def _append_bounded(
        self,
        values: list[Any],
        value: Any,
        *,
        deduplicate: bool,
    ) -> None:
        if deduplicate and isinstance(value, SessionEntity):
            values[:] = [
                item
                for item in values
                if not isinstance(item, SessionEntity) or item.identity != value.identity
            ]
        values.append(value)
        del values[: -self._history_limit]

    @staticmethod
    def _effect_succeeded(result: ToolResult) -> bool:
        if not result.ok:
            return False
        data = result.data or {}
        if data.get("verified") is False or data.get("success") is False:
            return False
        return result.evidence.get("verification_status") != "failed"

    @staticmethod
    def _is_coordination_tool(tool: str) -> bool:
        return tool.startswith("task_") or tool.startswith("activate_") or tool == "list_tool_capabilities"

    @classmethod
    def _target(
        cls, result: ToolResult, arguments: dict[str, Any] | None
    ) -> str | None:
        data = result.data or {}
        for key in ("target", "application", "path", "destination", "source", "url", "query", "name"):
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return cls._text(value, 500)
        if result.error is not None:
            for key in ("path", "application", "query", "target"):
                value = result.error.details.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return cls._text(value, 500)
        if arguments:
            for key in ("window", "application", "path", "destination", "source", "url", "query", "name"):
                value = arguments.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return cls._text(value, 500)
        return None

    @classmethod
    def _compact_arguments(cls, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            key: cls._compact_value(value)
            for key, value in arguments.items()
            if key in cls._SAFE_ARGUMENT_KEYS
        }

    @classmethod
    def _compact_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._compact_value(item)
                for key, item in list(value.items())[:8]
            }
        if isinstance(value, list):
            return [cls._compact_value(item) for item in value[:5]]
        return cls._text(value, 300) if isinstance(value, (str, Path)) else value

    @classmethod
    def _page_entity(cls, raw: dict[str, Any], tab_index: object) -> SessionEntity | None:
        url = cls._text(raw.get("url") or raw.get("active_url"), 1_000)
        if not url:
            return None
        index = tab_index if isinstance(tab_index, int) and tab_index >= 1 else None
        return SessionEntity(
            kind="page",
            title=cls._text(raw.get("title"), 300),
            url=url,
            tab_index=index,
        )

    @classmethod
    def _monitor_from(
        cls, raw: object, world: WorldStateSnapshot | None
    ) -> SessionMonitor | None:
        if isinstance(raw, dict):
            monitor_id = cls._text(raw.get("monitor_id"), 200)
            number = raw.get("number") or raw.get("display_number")
            label = raw.get("label") or raw.get("friendly_name")
            return SessionMonitor(
                monitor_id=monitor_id,
                number=number if isinstance(number, int) and number >= 1 else None,
                label=cls._text(label, 200)
                or (f"monitor {number}" if isinstance(number, int) else None),
            )
        return cls._monitor_from_id(None, world)

    @classmethod
    def _monitor_from_id(
        cls, monitor_id: str | None, world: WorldStateSnapshot | None
    ) -> SessionMonitor | None:
        if not monitor_id or world is None:
            return None
        for raw in world.monitor_layout:
            if str(raw.get("monitor_id") or "") != monitor_id:
                continue
            number = raw.get("number") or raw.get("display_number")
            label = raw.get("label") or raw.get("friendly_name")
            return SessionMonitor(
                monitor_id=monitor_id,
                number=number if isinstance(number, int) and number >= 1 else None,
                label=cls._text(label, 200)
                or (f"monitor {number}" if isinstance(number, int) else None),
            )
        return SessionMonitor(monitor_id=monitor_id)

    @staticmethod
    def _raw_monitor_id(raw_window: object) -> str | None:
        if not isinstance(raw_window, dict):
            return None
        value = raw_window.get("monitor_id")
        return str(value) if isinstance(value, str) and value else None

    @staticmethod
    def _path(value: object) -> str | None:
        if not isinstance(value, (str, Path)):
            return None
        text = str(value).strip()
        return text[:1_000] if text else None

    @staticmethod
    def _text(value: object, limit: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text[:limit] if text else None

    @staticmethod
    def _entity_label(entity: SessionEntity | None) -> str:
        if entity is None:
            return "None"
        return entity.name or entity.application or entity.title or entity.path or entity.url or "?"

    @staticmethod
    def _entity_context(entity: SessionEntity | None) -> dict[str, Any] | None:
        if entity is None:
            return None
        return entity.model_dump(exclude_none=True)

    @staticmethod
    def _monitor_context(monitor: SessionMonitor | None) -> dict[str, Any] | None:
        if monitor is None:
            return None
        # Backend IDs remain internal; the LLM uses Wyzer's semantic/user-facing numbering.
        return monitor.model_dump(exclude={"monitor_id"}, exclude_none=True)

    @staticmethod
    def _serialized_size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
