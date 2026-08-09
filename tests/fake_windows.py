"""Deterministic mutable Windows backend for Stage 4 tests."""

from pathlib import Path
from typing import Any

from wyzer.models import (
    MonitorDestination,
    MonitorInfo,
    ProcessInfo,
    Rect,
    WindowInfo,
    WindowMoveOutcome,
)
from wyzer.tools import ToolExecutionError


class FakeWindowsBackend:
    def __init__(self) -> None:
        self.processes = [ProcessInfo(process_id=10, name="explorer.exe")]
        self.monitors = [
            MonitorInfo(
                monitor_id="monitor:1",
                device_name="DISPLAY1",
                rectangle=Rect(left=0, top=0, right=1920, bottom=1080),
                work_area=Rect(left=0, top=0, right=1920, bottom=1040),
                primary=True,
                number=1,
                label="monitor 1",
                friendly_name="Fake Primary Display",
                relative_position="primary",
            ),
            MonitorInfo(
                monitor_id="monitor:2",
                device_name="DISPLAY2",
                rectangle=Rect(left=1920, top=0, right=3840, bottom=1080),
                work_area=Rect(left=1920, top=0, right=3840, bottom=1040),
                number=2,
                label="monitor 2",
                friendly_name="Fake Right Display",
                relative_position="right",
            ),
        ]
        self.windows = [
            WindowInfo(
                handle=100,
                title="Notes",
                process_id=10,
                application="notepad.exe",
                rectangle=Rect(left=10, top=10, right=810, bottom=610),
                monitor_id="monitor:1",
            )
        ]
        self.foreground_handle: int | None = 100
        self.previous_monitor_by_window: dict[int, str] = {}
        self.verify_actions = True
        self.verification_timeout_seconds = 0.01
        self.opened_files: list[Path] = []
        self.opened_websites: list[str] = []
        self.opened_browser_websites: list[tuple[str, str]] = []
        self.media_actions: list[str] = []
        self.master_audio = {"level": 65, "muted": False}
        self.audio_sessions: list[dict[str, Any]] = [
            {
                "session_id": "chrome:1",
                "name": "Google Chrome",
                "process": "chrome.exe",
                "process_id": 31,
                "volume": 60,
                "muted": False,
                "active": True,
                "multiple_sessions": False,
            }
        ]
        self.current_media_info: dict[str, str | bool | None] = {
            "available": True,
            "title": "Test Song",
            "artist": "Test Artist",
            "album": "Test Album",
            "source": "Test Player",
            "status": "playing",
        }
        self.ui_elements: list[dict[str, Any]] = [
            {
                "selector": "p:0",
                "name": "Search",
                "automation_id": "searchBox",
                "control_type": "EditControl",
                "class_name": "Edit",
                "value": "",
                "enabled": True,
                "focused": False,
                "offscreen": False,
                "selected": None,
                "toggle_state": None,
                "expand_collapse_state": None,
                "depth": 1,
                "rectangle": {"left": 10, "top": 10, "right": 200, "bottom": 40},
            },
            {
                "selector": "p:1",
                "name": "Play",
                "automation_id": "playButton",
                "control_type": "ButtonControl",
                "class_name": "Button",
                "value": None,
                "enabled": True,
                "focused": False,
                "offscreen": False,
                "selected": None,
                "toggle_state": None,
                "expand_collapse_state": None,
                "depth": 1,
                "rectangle": {"left": 10, "top": 50, "right": 90, "bottom": 80},
            },
            {
                "selector": "p:2",
                "name": "Shuffle",
                "automation_id": "shuffleToggle",
                "control_type": "CheckBoxControl",
                "class_name": "CheckBox",
                "value": None,
                "enabled": True,
                "focused": False,
                "offscreen": False,
                "selected": None,
                "toggle_state": "off",
                "expand_collapse_state": None,
                "depth": 1,
                "rectangle": {"left": 100, "top": 50, "right": 190, "bottom": 80},
            },
        ]
        self.volume_actions: list[str] = []

    def system_profile(self) -> dict[str, Any]:
        return {
            "computer_name": "TEST-PC",
            "operating_system": "Windows 11 Test",
            "architecture": "AMD64",
            "processor": "Test CPU",
            "physical_cpu_cores": 4,
            "logical_cpu_cores": 8,
            "memory_total_bytes": 16 * 1024**3,
            "memory_available_bytes": 8 * 1024**3,
            "drives": [
                {
                    "mountpoint": "C:\\",
                    "file_system": "NTFS",
                    "total_bytes": 512 * 1024**3,
                    "free_bytes": 256 * 1024**3,
                }
            ],
        }

    def diagnose_system(self, *, scope: str = "auto") -> dict[str, Any]:
        return {
            "scope": scope,
            "health": "attention",
            "collected_at": "2026-08-08T00:00:00+00:00",
            "summary": ["CPU 42.0%; memory 50.0% used."],
            "findings": [
                {
                    "severity": "attention",
                    "component": "event_log",
                    "message": "Windows logged one recent error.",
                    "evidence": {"count": 1},
                }
            ],
            "telemetry": {
                "performance": {
                    "cpu_percent": 42.0,
                    "memory": {"used_percent": 50.0},
                    "top_processes": [],
                }
            },
            "unavailable": [],
            "warnings": [],
            "evidence": {"collector": "fake"},
        }

    def list_processes(self) -> list[ProcessInfo]:
        return list(self.processes)

    def search_applications(self, query: str) -> list[dict[str, str]]:
        return [{"name": query, "source": "fake"}]

    def list_installed_applications(self) -> list[dict[str, str]]:
        return [
            {"name": "Calculator", "source": "fake"},
            {"name": "Test Game", "source": "fake"},
        ]

    def list_installed_games(self) -> list[dict[str, str]]:
        return [{"name": "Test Game", "source": "fake"}]

    def refresh_application_index(self) -> int:
        return 1

    def is_process_running(self, *, process_id: int | None = None, name: str | None = None) -> bool:
        if process_id is not None:
            return any(item.process_id == process_id for item in self.processes)
        return any(item.name.casefold() == (name or "").casefold() for item in self.processes)

    def launch_application(self, application: str) -> tuple[int | None, str]:
        if application == "missing":
            raise ToolExecutionError(
                "APPLICATION_NOT_FOUND",
                "Application was not found.",
                details={"application": application},
            )
        process_id = 20
        if self.verify_actions:
            self.processes.append(ProcessInfo(process_id=process_id, name=f"{application}.exe"))
            handle = max((window.handle for window in self.windows), default=199) + 1
            self.windows.append(
                WindowInfo(
                    handle=handle,
                    title=application,
                    process_id=process_id,
                    application=f"{application}.exe",
                    monitor_id="monitor:1",
                )
            )
        return process_id, f"{application}.exe"

    def open_file(self, path: Path) -> None:
        self.opened_files.append(path)
        if path.is_dir():
            handle = max((window.handle for window in self.windows), default=100) + 1
            self.windows.append(
                WindowInfo(
                    handle=handle,
                    title=path.name,
                    process_id=10,
                    application="explorer.exe",
                    monitor_id="monitor:1",
                )
            )

    def control_media(self, action: str) -> None:
        self.media_actions.append(action)

    def current_media(self) -> dict[str, str | bool | None]:
        return dict(self.current_media_info)

    def control_volume(self, action: str) -> None:
        self.volume_actions.append(action)

    def control_master_audio(
        self, operation: str, amount: int | None = None, level: int | None = None
    ) -> dict[str, Any]:
        previous = int(self.master_audio["level"])
        if operation == "increase":
            self.master_audio["level"] = min(100, previous + (amount or 10))
        elif operation == "decrease":
            self.master_audio["level"] = max(0, previous - (amount or 10))
        elif operation == "set":
            assert level is not None
            self.master_audio["level"] = level
        elif operation == "mute":
            self.master_audio["muted"] = True
        elif operation == "unmute":
            self.master_audio["muted"] = False
        elif operation == "toggle_mute":
            self.master_audio["muted"] = not bool(self.master_audio["muted"])
        return {
            "target": "master",
            "operation": operation,
            "previous_level": previous,
            "new_level": self.master_audio["level"],
            "muted": self.master_audio["muted"],
            "fallback_used": False,
        }

    def list_audio_sessions(self) -> dict[str, Any]:
        return {
            "sessions": [dict(item) for item in self.audio_sessions],
            "count": len(self.audio_sessions),
        }

    def control_application_audio(
        self,
        application: str,
        operation: str,
        amount: int | None = None,
        level: int | None = None,
        scope: str = "all",
    ) -> dict[str, Any]:
        del scope
        matches = [
            item
            for item in self.audio_sessions
            if application.casefold().removesuffix(".exe")
            in f"{item['name']} {item['process']}".casefold()
        ]
        if not matches:
            raise ToolExecutionError(
                "AUDIO_SESSION_NOT_FOUND",
                f"{application} does not currently have an active audio session.",
            )
        for item in matches:
            if operation == "increase":
                item["volume"] = min(100, int(item["volume"]) + (amount or 10))
            elif operation == "decrease":
                item["volume"] = max(0, int(item["volume"]) - (amount or 10))
            elif operation == "set":
                item["volume"] = level
            elif operation == "mute":
                item["muted"] = True
            elif operation == "unmute":
                item["muted"] = False
            elif operation == "toggle_mute":
                item["muted"] = not bool(item["muted"])
        return {
            "target": str(matches[0]["name"]),
            "matched_process": matches[0]["process"],
            "operation": operation,
            "requested_level": level,
            "sessions_matched": len(matches),
            "sessions_changed": len(matches),
            "resulting_levels": sorted({int(item["volume"]) for item in matches}),
            "muted": all(bool(item["muted"]) for item in matches),
            "session_ids": [str(item["session_id"]) for item in matches],
        }

    def mute_audio_sessions_except(self, applications: list[str]) -> dict[str, Any]:
        keep = {value.casefold().removesuffix(".exe") for value in applications}
        changed = 0
        for session in self.audio_sessions:
            identifier = f"{session['name']} {session['process']}".casefold()
            if not any(name in identifier for name in keep) and not session["muted"]:
                session["muted"] = True
                changed += 1
        return {
            "operation": "mute_all_except",
            "kept_applications": applications,
            "sessions_changed": changed,
            "sessions_excluded": len(self.audio_sessions) - changed,
        }

    def audio_diagnostic(self) -> dict[str, Any]:
        return {
            "output_device": "Fake speakers",
            "master": {"level": self.master_audio["level"], "muted": self.master_audio["muted"]},
            "sessions": self.list_audio_sessions()["sessions"],
        }

    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def find_windows(self, query: str) -> list[WindowInfo]:
        compact = "".join(character for character in query.casefold() if character.isalnum())
        compact = {"fileexplorer": "explorer"}.get(compact, compact)
        return [
            window
            for window in self.windows
            if compact
            in "".join(
                character
                for character in f"{window.title} {window.application or ''}".casefold()
                if character.isalnum()
            )
        ]

    def foreground_window(self) -> WindowInfo | None:
        return next(
            (window for window in self.windows if window.handle == self.foreground_handle), None
        )

    def focus_window(self, handle: int) -> bool:
        if self.verify_actions:
            self.foreground_handle = handle
        return self.verify_actions

    def minimize_window(self, handle: int) -> bool:
        self._replace(handle, minimized=True, maximized=False)
        return self.verify_actions

    def maximize_window(self, handle: int) -> bool:
        self._replace(handle, minimized=False, maximized=True)
        return self.verify_actions

    def restore_window(self, handle: int) -> bool:
        self._replace(handle, minimized=False, maximized=False)
        return self.verify_actions

    def close_window(self, handle: int, timeout_seconds: float = 3) -> bool:
        del timeout_seconds
        if self.verify_actions:
            self.windows = [window for window in self.windows if window.handle != handle]
        return self.verify_actions

    def move_window_to_monitor(
        self, handle: int, destination: MonitorDestination
    ) -> WindowMoveOutcome:
        current = next(window for window in self.windows if window.handle == handle)
        source = next(
            monitor for monitor in self.monitors if monitor.monitor_id == current.monitor_id
        )
        target: MonitorInfo | None = None
        if destination.number is not None:
            target = next(
                (monitor for monitor in self.monitors if monitor.number == destination.number),
                None,
            )
        elif destination.device_name is not None:
            normalized = destination.device_name.casefold()
            target = next(
                (
                    monitor
                    for monitor in self.monitors
                    if normalized
                    in {
                        monitor.device_name.casefold(),
                        (monitor.label or "").casefold(),
                    }
                ),
                None,
            )
        elif destination.relation == "previous":
            previous_id = self.previous_monitor_by_window.get(handle)
            target = next(
                (monitor for monitor in self.monitors if monitor.monitor_id == previous_id),
                None,
            )
        elif destination.relation == "primary":
            target = next(monitor for monitor in self.monitors if monitor.primary)
        elif destination.relation in {"other", "nearest"}:
            target = next(
                monitor for monitor in self.monitors if monitor.monitor_id != source.monitor_id
            )
        elif destination.relation == "right":
            target = next(
                (
                    monitor
                    for monitor in self.monitors
                    if monitor.rectangle.left >= source.rectangle.right
                ),
                None,
            )
        elif destination.relation == "left":
            target = next(
                (
                    monitor
                    for monitor in self.monitors
                    if monitor.rectangle.right <= source.rectangle.left
                ),
                None,
            )
        elif destination.relation == "above":
            target = next(
                (
                    monitor
                    for monitor in self.monitors
                    if monitor.rectangle.bottom <= source.rectangle.top
                ),
                None,
            )
        elif destination.relation == "below":
            target = next(
                (
                    monitor
                    for monitor in self.monitors
                    if monitor.rectangle.top >= source.rectangle.bottom
                ),
                None,
            )
        if target is None:
            raise ToolExecutionError("MONITOR_NOT_FOUND", "The requested monitor was not found.")
        changed = target.monitor_id != source.monitor_id
        if self.verify_actions and changed:
            rectangle = current.rectangle
            translated = None
            if rectangle is not None:
                width = rectangle.right - rectangle.left
                height = rectangle.bottom - rectangle.top
                translated = Rect(
                    left=target.work_area.left + 10,
                    top=target.work_area.top + 10,
                    right=target.work_area.left + 10 + width,
                    bottom=target.work_area.top + 10 + height,
                )
            self._replace(handle, monitor_id=target.monitor_id, rectangle=translated)
        observed = target if self.verify_actions else source
        if self.verify_actions and changed:
            self.previous_monitor_by_window[handle] = source.monitor_id
        return WindowMoveOutcome(
            verified=self.verify_actions,
            changed=changed and self.verify_actions,
            destination=destination,
            source_monitor=source,
            target_monitor=target,
            observed_monitor=observed,
            preserved_state=(
                "minimized" if current.minimized else "maximized" if current.maximized else "normal"
            ),
        )

    def list_monitors(self) -> list[MonitorInfo]:
        return list(self.monitors)

    def _replace(self, handle: int, **changes: object) -> None:
        self.windows = [
            window.model_copy(update=changes) if window.handle == handle else window
            for window in self.windows
        ]
