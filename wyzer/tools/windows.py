"""Typed Stage 4 tools backed by real Windows APIs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from wyzer.desktop.system import WindowsSystemBackend
from wyzer.models import (
    MonitorDestination,
    MonitorInfo,
    ProcessInfo,
    RiskLevel,
    ToolArguments,
    WindowInfo,
)
from wyzer.tools.base import Tool, ToolContext, ToolExecutionError


def _evidence(status: str, predicate: str, observed: dict[str, Any]) -> dict[str, Any]:
    return {"verification_status": status, "predicate": predicate, "observed": observed}


def _prefer_direct_application_windows(windows: list[WindowInfo], query: str) -> list[WindowInfo]:
    """Resolve Calculator by application identity, not incidental title text.

    Windows 11 can expose one visible Calculator UI through both ``CalculatorApp.exe`` and
    ``ApplicationFrameHost.exe``. File Explorer can also contain the word ``Calculator`` in a
    filename, which must never make Explorer a Calculator target. Keep this deliberately narrow
    so matching behavior for other applications is unchanged and multiple genuine Calculator
    windows remain ambiguous.
    """
    compact_query = "".join(character for character in query.casefold() if character.isalnum())
    if compact_query not in {"calculator", "calc"}:
        return windows

    def application_identity(window: WindowInfo) -> str:
        return "".join(
            character
            for character in (window.application or "").casefold().removesuffix(".exe")
            if character.isalnum()
        )

    calculator_identities = {"calculator", "calculatorapp", "calc"}
    direct = [window for window in windows if application_identity(window) in calculator_identities]
    if direct:
        return direct

    # Some Windows builds expose Calculator only through ApplicationFrameHost. Accept that
    # wrapper only when its visible title is exactly Calculator; reject Explorer windows whose
    # filename merely happens to contain the word.
    wrappers = [
        window
        for window in windows
        if application_identity(window) == "applicationframehost"
        and "".join(character for character in window.title.casefold() if character.isalnum())
        == "calculator"
    ]
    return wrappers


class NoArguments(ToolArguments):
    pass


class ListWindowsArguments(ToolArguments):
    monitor: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional monitor such as 1, 2, left, right, or a display name.",
    )
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=260,
        description="Optional application name or visible window-title text.",
    )


class ProcessQueryArguments(ToolArguments):
    process_id: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_one_query(self) -> ProcessQueryArguments:
        if (self.process_id is None) == (self.name is None):
            raise ValueError("provide exactly one of process_id or name")
        return self


class OpenApplicationArguments(ToolArguments):
    application: str = Field(
        min_length=1,
        max_length=260,
        description="Friendly installed application name, such as Calculator, Spotify, or Discord.",
    )


class ApplicationSearchArguments(ToolArguments):
    query: str = Field(
        min_length=1,
        max_length=260,
        description="Application name or partial name to search for.",
    )


class OpenFileArguments(ToolArguments):
    path: Path = Field(
        description="Exact local file path to open with its default Windows application."
    )


class MediaControlArguments(ToolArguments):
    action: Literal["play_pause", "next", "previous", "stop"] = Field(
        description="Media action to send to the current system media session."
    )


class MasterAudioArguments(ToolArguments):
    operation: Literal["increase", "decrease", "set", "mute", "unmute", "toggle_mute", "get"] = (
        Field(description="Audio operation to perform.")
    )
    amount: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Percentage-point change for increase or decrease.",
    )
    level: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Exact volume percentage for set.",
    )

    @model_validator(mode="after")
    def validate_operation(self) -> MasterAudioArguments:
        if self.operation == "set" and self.level is None:
            raise ValueError("level is required when operation is set")
        if self.operation == "set" and self.amount is not None:
            raise ValueError("amount is not valid when operation is set")
        if self.operation in {"increase", "decrease"} and self.level is not None:
            raise ValueError("level is only valid when operation is set")
        if self.operation not in {"increase", "decrease", "set"} and (
            self.amount is not None or self.level is not None
        ):
            raise ValueError("amount and level are not valid for this operation")
        return self


class ApplicationAudioArguments(MasterAudioArguments):
    application: str = Field(
        min_length=1,
        max_length=260,
        description="Friendly application or audio-session name, such as Spotify or Chrome.",
    )
    scope: Literal["one", "all"] = Field(
        default="all",
        description="Use all for every matching audio session; use one for a single match.",
    )


class AudioSessionsBatchArguments(ToolArguments):
    applications: list[str] = Field(
        min_length=1,
        max_length=20,
        description="Application names that should remain unmuted; every other audio session is muted.",
    )


class WaitArguments(ToolArguments):
    milliseconds: int = Field(ge=0, le=60_000)


def _legacy_monitor_destination(value: object) -> object:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return value
    normalized = " ".join(value.strip().casefold().split())
    relations = {
        "other": "other",
        "the other": "other",
        "other monitor": "other",
        "primary": "primary",
        "primary monitor": "primary",
        "main": "primary",
        "main monitor": "primary",
        "left": "left",
        "left monitor": "left",
        "right": "right",
        "right monitor": "right",
        "above": "above",
        "upper": "above",
        "top": "above",
        "below": "below",
        "lower": "below",
        "bottom": "below",
        "nearest": "nearest",
        "closest": "nearest",
        "previous": "previous",
        "previous monitor": "previous",
        "back": "previous",
    }
    relation = relations.get(normalized)
    if relation is not None:
        return {"relation": relation}
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
    }
    compact = normalized.removeprefix("monitor ").removeprefix("display ")
    number = words.get(compact)
    if number is None and compact.isdigit():
        number = int(compact)
    return {"number": number} if number is not None else {"device_name": value.strip()}


def _normalize_destination_arguments(value: object) -> object:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    if "destination" not in normalized and "monitor" in normalized:
        normalized["destination"] = _legacy_monitor_destination(normalized.pop("monitor"))
    elif "destination" in normalized:
        normalized["destination"] = _legacy_monitor_destination(normalized["destination"])
    return normalized


class MoveNamedWindowArguments(ToolArguments):
    window: str = Field(
        min_length=1,
        max_length=260,
        description="Application name or visible title of exactly one open window.",
    )
    destination: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Monitor destination such as other, primary, left, right, above, below, nearest, "
            "previous, monitor 1, monitor 2, or an exact friendly display name."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_monitor(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "destination" not in normalized and "monitor" in normalized:
            normalized["destination"] = normalized.pop("monitor")
        destination = normalized.get("destination")
        if isinstance(destination, dict):
            if destination.get("relation") is not None:
                normalized["destination"] = str(destination["relation"])
            elif destination.get("number") is not None:
                normalized["destination"] = f"monitor {destination['number']}"
            elif destination.get("device_name") is not None:
                normalized["destination"] = str(destination["device_name"])
        return normalized

    def resolved_destination(self) -> MonitorDestination:
        resolved = _legacy_monitor_destination(self.destination)
        if not isinstance(resolved, dict):
            raise ValueError("invalid monitor destination")
        return MonitorDestination.model_validate(resolved)


class NamedWindowActionArguments(ToolArguments):
    window: str = Field(
        min_length=1,
        max_length=260,
        description="Application name or visible window-title text.",
    )
    action: Literal["focus", "minimize", "maximize", "restore", "close"] = Field(
        description="Window action to perform."
    )
    all_matches: bool = Field(
        default=False,
        description="Apply the action to every matching window instead of requiring one unique match.",
    )


class ProcessesResult(BaseModel):
    processes: list[ProcessInfo]
    count: int


class SystemProfileResult(BaseModel):
    computer_name: str
    operating_system: str
    architecture: str
    processor: str | None = None
    physical_cpu_cores: int | None = None
    logical_cpu_cores: int | None = None
    graphics_adapters: list[str] = Field(default_factory=list)
    memory_total_bytes: int
    memory_available_bytes: int
    drives: list[dict[str, Any]] = Field(default_factory=list)


class ApplicationSearchResult(BaseModel):
    applications: list[dict[str, str]]
    count: int


class InstalledGamesResult(BaseModel):
    games: list[dict[str, str]]
    count: int


class ApplicationIndexResult(BaseModel):
    count: int


class ProcessRunningResult(BaseModel):
    running: bool
    process_id: int | None = None
    name: str | None = None
    status: str = Field(pattern=r"^(open|minimized|background|not_running)$")
    windows: list[WindowInfo] = Field(default_factory=list)
    matched_processes: list[ProcessInfo] = Field(default_factory=list)
    evidence: dict[str, Any]


class BringUpApplicationResult(BaseModel):
    application: str
    outcome: str = Field(pattern=r"^(focused|opened)$")
    process_id: int | None = None
    window: WindowInfo | None = None
    verified: bool
    evidence: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class OpenTargetResult(BaseModel):
    target: str
    target_kind: str | None = None
    window: WindowInfo | None = None
    command_sent: bool
    verified: bool
    evidence: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class CurrentMediaResult(BaseModel):
    available: bool
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    source: str | None = None
    status: str | None = None


class MasterAudioResult(BaseModel):
    target: str
    operation: str
    previous_level: int | None = Field(default=None, ge=0, le=100)
    new_level: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None
    fallback_used: bool = False
    warnings: list[str] = Field(default_factory=list)


class AudioSessionsResult(BaseModel):
    sessions: list[dict[str, Any]]
    count: int


class ApplicationAudioResult(BaseModel):
    target: str
    matched_process: str | None = None
    operation: str
    requested_level: int | None = Field(default=None, ge=0, le=100)
    sessions_matched: int = Field(ge=0)
    sessions_changed: int = Field(ge=0)
    resulting_levels: list[int] = Field(default_factory=list)
    muted: bool | None = None
    session_ids: list[str] = Field(default_factory=list)


class AudioBatchResult(BaseModel):
    operation: str
    kept_applications: list[str]
    sessions_changed: int = Field(ge=0)
    sessions_excluded: int = Field(ge=0)


class WaitResult(BaseModel):
    elapsed_ms: int


class WindowResult(BaseModel):
    window: WindowInfo | None


class WindowsResult(BaseModel):
    windows: list[WindowInfo]
    count: int
    monitor: MonitorInfo | None = None
    monitor_number: int | None = None
    query: str | None = None


class WindowActionResult(BaseModel):
    window_handle: int
    window_handles: list[int] = Field(default_factory=list)
    target: str | None = None
    verified: bool
    window: WindowInfo | None = None
    destination: MonitorDestination | None = None
    source_monitor: MonitorInfo | None = None
    target_monitor: MonitorInfo | None = None
    observed_monitor: MonitorInfo | None = None
    changed_monitor: bool | None = None
    source_label: str | None = None
    destination_label: str | None = None
    changed: bool | None = None
    evidence: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class MonitorsResult(BaseModel):
    monitors: list[MonitorInfo]
    count: int


class WindowsToolBase:
    backend: WindowsSystemBackend
    unavailable_reason: str | None = None
    available: bool = True
    default_timeout_seconds: float = 15.0

    def __init__(self, backend: WindowsSystemBackend) -> None:
        self.backend = backend
        if getattr(self, "name", "") in {
            "control_master_audio",
            "list_audio_sessions",
            "control_application_audio",
            "mute_all_audio_except",
        }:
            configured = getattr(backend, "audio_timeout_seconds", self.default_timeout_seconds)
            if isinstance(configured, (int, float)):
                self.default_timeout_seconds = float(configured)


class ListRunningProcessesTool(WindowsToolBase, Tool[NoArguments, ProcessesResult]):
    llm_visible = False
    name = "list_running_processes"
    description = "List running Windows processes with stable process identifiers."
    arguments_type = NoArguments
    result_type = ProcessesResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> ProcessesResult:
        del arguments, context
        processes = self.backend.list_processes()
        return ProcessesResult(processes=processes, count=len(processes))


class GetSystemProfileTool(WindowsToolBase, Tool[NoArguments, SystemProfileResult]):
    name = "get_system_profile"
    description = (
        "Inspect the current computer's Windows version, CPU, memory, architecture, and drives."
    )
    arguments_type = NoArguments
    result_type = SystemProfileResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> SystemProfileResult:
        del arguments, context
        return SystemProfileResult.model_validate(self.backend.system_profile())


class IsProcessRunningTool(WindowsToolBase, Tool[ProcessQueryArguments, ProcessRunningResult]):
    name = "is_process_running"
    description = (
        "Check whether an application or process is currently present. Friendly application "
        "names are allowed; open and minimized windows count as running, and a process with no "
        "window is reported as background."
    )
    arguments_type = ProcessQueryArguments
    result_type = ProcessRunningResult
    risk_level = RiskLevel.LOW
    read_only = True

    @staticmethod
    def _compact(value: str) -> str:
        compact = "".join(character for character in value.casefold() if character.isalnum())
        return compact.removesuffix("exe")

    @classmethod
    def _process_matches(cls, process: ProcessInfo, identities: set[str]) -> bool:
        observed = cls._compact(process.name)
        if not observed:
            return False
        for identity in identities:
            if not identity:
                continue
            if observed == identity:
                return True
            # Windows packaged applications frequently append a stable suffix, for example
            # Calculator -> CalculatorApp.exe. Keep this deliberately conservative so a short
            # query such as "app" cannot match unrelated processes.
            if len(identity) >= 5 and observed.startswith(identity):
                suffix = observed[len(identity) :]
                if suffix in {"app", "application", "desktop", "client", "launcher"}:
                    return True
        return False

    def execute(
        self, arguments: ProcessQueryArguments, context: ToolContext
    ) -> ProcessRunningResult:
        del context
        if arguments.process_id is not None:
            running = self.backend.is_process_running(process_id=arguments.process_id)
            matched = [
                process
                for process in self.backend.list_processes()
                if process.process_id == arguments.process_id
            ]
            return ProcessRunningResult(
                running=running,
                process_id=arguments.process_id,
                status="background" if running else "not_running",
                matched_processes=matched,
                evidence=_evidence(
                    "verified",
                    "process_running",
                    {"running": running, "process_id": arguments.process_id},
                ),
            )

        assert arguments.name is not None
        requested = arguments.name.strip()
        windows = _prefer_direct_application_windows(
            self.backend.find_windows(requested), requested
        )
        identities = {self._compact(requested)}
        try:
            for application in self.backend.search_applications(requested)[:5]:
                name = application.get("name")
                if isinstance(name, str):
                    identities.add(self._compact(name))
        except (AttributeError, NotImplementedError):
            pass
        processes = [
            process
            for process in self.backend.list_processes()
            if self._process_matches(process, identities)
            or any(process.process_id == window.process_id for window in windows)
        ]
        running = bool(windows or processes)
        if windows:
            status = "minimized" if all(window.minimized for window in windows) else "open"
        else:
            status = "background" if processes else "not_running"
        return ProcessRunningResult(
            running=running,
            name=requested,
            status=status,
            windows=windows,
            matched_processes=processes,
            evidence=_evidence(
                "verified",
                "application_present",
                {
                    "running": running,
                    "status": status,
                    "window_count": len(windows),
                    "process_ids": [process.process_id for process in processes],
                },
            ),
        )


class OpenApplicationTool(
    WindowsToolBase, Tool[OpenApplicationArguments, BringUpApplicationResult]
):
    name = "open_application"
    description = (
        "Open or focus a Windows desktop application. Reuse an existing window when possible "
        "and launch the application only when needed. Do not use this for webpage navigation "
        "or browser searches; use the browser tool pack for those tasks."
    )
    arguments_type = OpenApplicationArguments
    result_type = BringUpApplicationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    default_timeout_seconds = 15

    @staticmethod
    def _select_window(windows: list[WindowInfo], application: str) -> WindowInfo | None:
        windows = _prefer_direct_application_windows(windows, application)
        if not windows:
            return None
        query = application.casefold().strip().removesuffix(".exe")
        exact = [
            window
            for window in windows
            if query
            in {
                window.title.casefold().strip(),
                (window.application or "").casefold().strip().removesuffix(".exe"),
            }
        ]
        return (exact or windows)[0]

    def _focus_and_verify(
        self,
        application: str,
        window: WindowInfo,
        *,
        outcome: str,
        process_id: int | None,
    ) -> BringUpApplicationResult:
        if window.minimized:
            self.backend.restore_window(window.handle)
        self.backend.focus_window(window.handle)
        windows = self.backend.list_windows()
        updated = next((item for item in windows if item.handle == window.handle), None)
        foreground = self.backend.foreground_window()
        visible_window = updated or (
            foreground if foreground is not None and foreground.handle == window.handle else None
        )
        verified = (
            visible_window is not None
            and not visible_window.minimized
            and foreground is not None
            and foreground.handle == window.handle
        )
        return BringUpApplicationResult(
            application=application,
            outcome=outcome,
            # The process returned by ShellExecute/CreateProcess can be a short-lived
            # launcher.  Report the PID that owns the window we actually verified.
            process_id=visible_window.process_id
            if visible_window is not None
            else window.process_id,
            window=visible_window,
            verified=verified,
            evidence=_evidence(
                "verified" if verified else "not_verified",
                "application_window_visible_and_focused",
                {
                    "application": application,
                    "outcome": outcome,
                    "window_handle": window.handle,
                    "window_title": visible_window.title if visible_window else window.title,
                    "foreground_handle": foreground.handle if foreground else None,
                    "minimized": visible_window.minimized if visible_window else None,
                },
            ),
            warnings=(
                []
                if verified
                else ["The application window was found, but it was not confirmed in front."]
            ),
        )

    def execute(
        self, arguments: OpenApplicationArguments, context: ToolContext
    ) -> BringUpApplicationResult:
        del context
        existing = self._select_window(
            self.backend.find_windows(arguments.application), arguments.application
        )
        if existing is not None:
            return self._focus_and_verify(
                arguments.application,
                existing,
                outcome="focused",
                process_id=existing.process_id,
            )

        before_windows = self.backend.list_windows()
        before_handles = {window.handle for window in before_windows}
        previous_foreground = self.backend.foreground_window()
        process_id, executable = self.backend.launch_application(arguments.application)
        verification_timeout = float(getattr(self.backend, "verification_timeout_seconds", 8.0))
        deadline = time.monotonic() + verification_timeout
        matched_window: WindowInfo | None = None
        while True:
            windows = self.backend.list_windows()
            named_matches = self.backend.find_windows(arguments.application)
            matched_window = self._select_window(named_matches, arguments.application)
            if matched_window is None:
                executable_name = executable.casefold().removesuffix(".exe")
                launched_windows = [
                    window
                    for window in windows
                    if window.handle not in before_handles
                    and (
                        (process_id is not None and window.process_id == process_id)
                        or executable_name
                        in (window.application or "").casefold().removesuffix(".exe")
                        or arguments.application.casefold() in window.title.casefold()
                    )
                ]
                matched_window = self._select_window(launched_windows, arguments.application)
            if matched_window is None:
                foreground = self.backend.foreground_window()
                if (
                    foreground is not None
                    and foreground.handle not in before_handles
                    and (
                        previous_foreground is None
                        or foreground.handle != previous_foreground.handle
                    )
                ):
                    matched_window = foreground
            if matched_window is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        if matched_window is not None:
            return self._focus_and_verify(
                arguments.application,
                matched_window,
                outcome="opened",
                process_id=process_id,
            )
        return BringUpApplicationResult(
            application=arguments.application,
            outcome="opened",
            process_id=process_id,
            window=None,
            verified=False,
            evidence=_evidence(
                "not_verified",
                "application_window_visible_and_focused",
                {
                    "application": arguments.application,
                    "outcome": "opened",
                    "window_handle": None,
                    "foreground_handle": None,
                    "minimized": None,
                },
            ),
            warnings=[
                "The application was launched, but no visible foreground window was confirmed."
            ],
        )


class SearchInstalledApplicationsTool(
    WindowsToolBase, Tool[ApplicationSearchArguments, ApplicationSearchResult]
):
    name = "search_installed_applications"
    description = "Search indexed Start Menu, Xbox/Game Pass, Store, Steam, and Epic applications."
    arguments_type = ApplicationSearchArguments
    result_type = ApplicationSearchResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(
        self, arguments: ApplicationSearchArguments, context: ToolContext
    ) -> ApplicationSearchResult:
        del context
        applications = self.backend.search_applications(arguments.query)
        return ApplicationSearchResult(applications=applications, count=len(applications))


class RefreshApplicationIndexTool(WindowsToolBase, Tool[NoArguments, ApplicationIndexResult]):
    llm_visible = False
    name = "refresh_application_index"
    description = "Refresh installed application and game indexes across all mounted drives."
    arguments_type = NoArguments
    result_type = ApplicationIndexResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> ApplicationIndexResult:
        del arguments, context
        return ApplicationIndexResult(count=self.backend.refresh_application_index())


class ListInstalledGamesTool(WindowsToolBase, Tool[NoArguments, InstalledGamesResult]):
    name = "list_installed_games"
    description = "List installed games discovered from Xbox, Steam, Epic, EA, and Battle.net."
    arguments_type = NoArguments
    result_type = InstalledGamesResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> InstalledGamesResult:
        del arguments, context
        games = self.backend.list_installed_games()
        return InstalledGamesResult(games=games, count=len(games))


class ListInstalledApplicationsTool(WindowsToolBase, Tool[NoArguments, ApplicationSearchResult]):
    llm_visible = False
    name = "list_installed_applications"
    description = "List applications currently present in Wyzer's installed application index."
    arguments_type = NoArguments
    result_type = ApplicationSearchResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> ApplicationSearchResult:
        del arguments, context
        applications = self.backend.list_installed_applications()
        return ApplicationSearchResult(applications=applications, count=len(applications))


class OpenFileTool(WindowsToolBase, Tool[OpenFileArguments, OpenTargetResult]):
    name = "open_file"
    description = "Ask Windows to open an existing file with its associated application."
    arguments_type = OpenFileArguments
    result_type = OpenTargetResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: OpenFileArguments, context: ToolContext) -> OpenTargetResult:
        del context
        path = arguments.path.expanduser().resolve()
        before_handles = {window.handle for window in self.backend.list_windows()}
        self.backend.open_file(path)
        matched_window: WindowInfo | None = None
        if path.is_dir():
            deadline = time.monotonic() + float(
                getattr(self.backend, "verification_timeout_seconds", 2.0)
            )
            wanted = path.name.casefold()
            while time.monotonic() < deadline:
                windows = self.backend.list_windows()
                candidates = [
                    window
                    for window in windows
                    if (window.application or "").casefold().removesuffix(".exe") == "explorer"
                    and wanted in window.title.casefold()
                ]
                new_candidates = [
                    window for window in candidates if window.handle not in before_handles
                ]
                if len(new_candidates) == 1:
                    matched_window = new_candidates[0]
                    break
                if len(candidates) == 1:
                    matched_window = candidates[0]
                    break
                time.sleep(0.05)
        verified = matched_window is not None
        return OpenTargetResult(
            target=str(path),
            target_kind="folder" if path.is_dir() else "file",
            window=matched_window,
            command_sent=True,
            verified=verified,
            evidence=_evidence(
                "verified" if verified else "unavailable",
                "folder_window_exists" if path.is_dir() else "file_opened",
                {
                    "file_exists": path.exists(),
                    "window_handle": matched_window.handle if matched_window else None,
                },
            ),
            warnings=(
                []
                if verified
                else [
                    "Windows accepted the request, but the associated application was not verified."
                ]
            ),
        )


def _unverified_command(target: str) -> OpenTargetResult:
    return OpenTargetResult(
        target=target,
        command_sent=True,
        verified=False,
        evidence=_evidence("unavailable", "command_effect_observed", {}),
        warnings=["The command was sent, but its resulting state was not verified."],
    )


class ControlMediaTool(WindowsToolBase, Tool[MediaControlArguments, OpenTargetResult]):
    name = "control_media"
    description = "Control active Windows media: play/pause, next, previous, or stop."
    arguments_type = MediaControlArguments
    result_type = OpenTargetResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: MediaControlArguments, context: ToolContext) -> OpenTargetResult:
        del context
        self.backend.control_media(arguments.action)
        return _unverified_command(arguments.action)


class GetCurrentMediaTool(WindowsToolBase, Tool[NoArguments, CurrentMediaResult]):
    name = "get_current_media"
    description = (
        "Read the current Windows media session's song, artist, album, source, and status."
    )
    arguments_type = NoArguments
    result_type = CurrentMediaResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> CurrentMediaResult:
        del arguments, context
        return CurrentMediaResult.model_validate(self.backend.current_media())


class ControlMasterAudioTool(WindowsToolBase, Tool[MasterAudioArguments, MasterAudioResult]):
    name = "control_master_audio"
    description = (
        "Read or control master Windows output audio. increase/decrease are relative percentage "
        "points; set is an exact 0-100 level. Use this for unqualified requests such as 'turn the "
        "volume down', not for a named application's volume."
    )
    arguments_type = MasterAudioArguments
    result_type = MasterAudioResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: MasterAudioArguments, context: ToolContext) -> MasterAudioResult:
        del context
        return MasterAudioResult.model_validate(
            self.backend.control_master_audio(
                arguments.operation, arguments.amount, arguments.level
            )
        )


class ListAudioSessionsTool(WindowsToolBase, Tool[NoArguments, AudioSessionsResult]):
    name = "list_audio_sessions"
    description = (
        "List active Windows audio sessions with compact names, processes, IDs, levels, and mute "
        "state. Use this when a requested application's audio session is uncertain."
    )
    arguments_type = NoArguments
    result_type = AudioSessionsResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> AudioSessionsResult:
        del arguments, context
        return AudioSessionsResult.model_validate(self.backend.list_audio_sessions())


class ControlApplicationAudioTool(
    WindowsToolBase, Tool[ApplicationAudioArguments, ApplicationAudioResult]
):
    name = "control_application_audio"
    description = (
        "Control a named application's audio session. increase/decrease are relative percentage "
        "points; set is exact 0-100. A named app means application audio, not master audio. Use "
        "list_audio_sessions when the target is uncertain."
    )
    arguments_type = ApplicationAudioArguments
    result_type = ApplicationAudioResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: ApplicationAudioArguments, context: ToolContext
    ) -> ApplicationAudioResult:
        del context
        return ApplicationAudioResult.model_validate(
            self.backend.control_application_audio(
                arguments.application,
                arguments.operation,
                arguments.amount,
                arguments.level,
                arguments.scope,
            )
        )


class MuteAllAudioExceptTool(WindowsToolBase, Tool[AudioSessionsBatchArguments, AudioBatchResult]):
    name = "mute_all_audio_except"
    description = (
        "Apply a safe deterministic batch audio operation. mute_all_except mutes every current "
        "audio session except sessions belonging to the named applications."
    )
    arguments_type = AudioSessionsBatchArguments
    result_type = AudioBatchResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: AudioSessionsBatchArguments, context: ToolContext
    ) -> AudioBatchResult:
        del context
        return AudioBatchResult.model_validate(
            self.backend.mute_audio_sessions_except(arguments.applications)
        )


class WaitMsTool(Tool[WaitArguments, WaitResult]):
    llm_visible = False
    name = "wait_ms"
    description = "Wait for a bounded number of milliseconds without changing Windows state."
    arguments_type = WaitArguments
    result_type = WaitResult
    risk_level = RiskLevel.LOW
    read_only = True
    default_timeout_seconds = 65

    def execute(self, arguments: WaitArguments, context: ToolContext) -> WaitResult:
        del context
        started = time.monotonic()
        time.sleep(arguments.milliseconds / 1000)
        return WaitResult(elapsed_ms=round((time.monotonic() - started) * 1000))


class GetForegroundWindowTool(WindowsToolBase, Tool[NoArguments, WindowResult]):
    name = "get_foreground_window"
    description = "Return the current foreground window from Win32."
    arguments_type = NoArguments
    result_type = WindowResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> WindowResult:
        del arguments, context
        return WindowResult(window=self.backend.foreground_window())


class ListOpenWindowsTool(WindowsToolBase, Tool[ListWindowsArguments, WindowsResult]):
    name = "list_open_windows"
    description = (
        "Live-check open desktop windows, optionally filtered by application/title and monitor. "
        "Minimized windows are still open and are included."
    )
    arguments_type = ListWindowsArguments
    result_type = WindowsResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: ListWindowsArguments, context: ToolContext) -> WindowsResult:
        del context
        windows = (
            _prefer_direct_application_windows(
                self.backend.find_windows(arguments.query), arguments.query
            )
            if arguments.query is not None
            else self.backend.list_windows()
        )
        selected_monitor: MonitorInfo | None = None
        monitor_number: int | None = None
        if arguments.monitor is not None:
            monitors = self.backend.list_monitors()
            requested = arguments.monitor.strip().casefold()
            spoken_numbers = {
                "one": 1,
                "won": 1,
                "two": 2,
                "too": 2,
                "to": 2,
                "three": 3,
                "four": 4,
            }
            index = spoken_numbers.get(requested)
            if index is None and requested.isdigit():
                index = int(requested)
            if index is not None and 1 <= index <= len(monitors):
                selected_monitor = monitors[index - 1]
            if selected_monitor is None:
                selected_monitor = next(
                    (
                        monitor
                        for monitor in monitors
                        if requested
                        in {monitor.monitor_id.casefold(), monitor.device_name.casefold()}
                    ),
                    None,
                )
            if selected_monitor is None:
                raise ToolExecutionError(
                    "MONITOR_NOT_FOUND",
                    "The requested monitor could not be found.",
                    details={"monitor": arguments.monitor, "available": len(monitors)},
                )
            monitor_number = monitors.index(selected_monitor) + 1
            windows = [
                window for window in windows if window.monitor_id == selected_monitor.monitor_id
            ]
        return WindowsResult(
            windows=windows,
            count=len(windows),
            monitor=selected_monitor,
            monitor_number=monitor_number,
            query=arguments.query,
        )


class MoveNamedWindowToMonitorTool(
    WindowsToolBase, Tool[MoveNamedWindowArguments, WindowActionResult]
):
    name = "move_named_window_to_monitor"
    description = (
        "Find one live window, then move it by Windows display position: other, primary, left, "
        "right, above, below, nearest, previous, monitor number, or exact display device."
    )
    arguments_type = MoveNamedWindowArguments
    result_type = WindowActionResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: MoveNamedWindowArguments, context: ToolContext
    ) -> WindowActionResult:
        del context
        lookup_timeout = min(1.5, float(getattr(self.backend, "verification_timeout_seconds", 1.5)))
        deadline = time.monotonic() + lookup_timeout
        matches = self.backend.find_windows(arguments.window)
        while not matches and time.monotonic() < deadline:
            time.sleep(0.05)
            matches = self.backend.find_windows(arguments.window)
        matches = _prefer_direct_application_windows(matches, arguments.window)
        if not matches:
            raise ToolExecutionError(
                "WINDOW_NOT_FOUND", f"No open window matched {arguments.window}."
            )
        if len(matches) > 1:
            raise ToolExecutionError(
                "AMBIGUOUS_WINDOW",
                f"More than one open window matched {arguments.window}.",
                details={"matches": [window.title for window in matches[:10]]},
            )
        selected = matches[0]
        destination = arguments.resolved_destination()
        outcome = self.backend.move_window_to_monitor(selected.handle, destination)
        updated = next(
            (item for item in self.backend.list_windows() if item.handle == selected.handle), None
        )
        return WindowActionResult(
            window_handle=selected.handle,
            target=(selected.application or selected.title),
            verified=outcome.verified,
            window=updated,
            destination=outcome.destination,
            source_monitor=outcome.source_monitor,
            target_monitor=outcome.target_monitor,
            observed_monitor=outcome.observed_monitor,
            changed_monitor=outcome.changed,
            source_label=outcome.source_monitor.label,
            destination_label=outcome.target_monitor.label,
            changed=outcome.changed,
            evidence=_evidence(
                "verified" if outcome.verified else "not_verified",
                "named_window_on_monitor",
                {
                    "window": selected.title,
                    "destination": outcome.destination.model_dump(mode="json"),
                    "source_monitor": outcome.source_monitor.model_dump(mode="json"),
                    "target_monitor": outcome.target_monitor.model_dump(mode="json"),
                    "observed_monitor": (
                        outcome.observed_monitor.model_dump(mode="json")
                        if outcome.observed_monitor
                        else None
                    ),
                    "changed": outcome.changed,
                    "preserved_state": outcome.preserved_state,
                },
            ),
            warnings=[]
            if outcome.verified
            else ["The window did not reach the resolved monitor destination."],
        )


class GetMonitorLayoutTool(WindowsToolBase, Tool[NoArguments, MonitorsResult]):
    name = "get_monitor_layout"
    description = (
        "Return the active Windows display topology: monitor numbers, friendly names, physical "
        "bounds, work areas, primary status, and positions relative to the primary monitor."
    )
    arguments_type = NoArguments
    result_type = MonitorsResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: NoArguments, context: ToolContext) -> MonitorsResult:
        del arguments, context
        monitors = self.backend.list_monitors()
        return MonitorsResult(monitors=monitors, count=len(monitors))


class ControlNamedWindowTool(WindowsToolBase, Tool[NamedWindowActionArguments, WindowActionResult]):
    name = "control_named_window"
    description = (
        "Focus, minimize, maximize, restore, or close a normal desktop window by title or app "
        "name. Do not use this to close Wyzer's managed Chrome/Edge automation browser; use "
        "browser_stop for that."
    )
    arguments_type = NamedWindowActionArguments
    result_type = WindowActionResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: NamedWindowActionArguments, context: ToolContext
    ) -> WindowActionResult:
        del context
        query = arguments.window.casefold().strip()
        lookup_timeout = min(1.5, float(getattr(self.backend, "verification_timeout_seconds", 1.5)))
        deadline = time.monotonic() + lookup_timeout
        candidates = self.backend.find_windows(query)
        while not candidates and time.monotonic() < deadline:
            time.sleep(0.05)
            candidates = self.backend.find_windows(query)
        candidates = _prefer_direct_application_windows(candidates, query)
        compact_query = "".join(character for character in query if character.isalnum())
        compact_workspace = "".join(
            character for character in Path.cwd().name.casefold() if character.isalnum()
        )
        if not candidates and compact_query == compact_workspace:
            foreground = self.backend.foreground_window()
            terminal_apps = {"windowsterminal", "powershell", "pwsh", "cmd", "python"}
            foreground_app = "".join(
                character
                for character in ((foreground.application or "") if foreground else "").casefold()
                if character.isalnum()
            ).removesuffix("exe")
            foreground_title = (foreground.title if foreground else "").casefold()
            if foreground is not None and (
                query in foreground_title or foreground_app in terminal_apps
            ):
                candidates = [foreground]
        exact = [
            window
            for window in candidates
            if query
            in {
                window.title.casefold(),
                (window.application or "").casefold().removesuffix(".exe"),
            }
        ]
        matches = exact or candidates
        if not matches:
            raise ToolExecutionError(
                "WINDOW_NOT_FOUND", f"No open window matched {arguments.window}."
            )
        if len(matches) > 1 and not arguments.all_matches:
            raise ToolExecutionError(
                "AMBIGUOUS_WINDOW",
                f"More than one open window matched {arguments.window}.",
                details={
                    "matches": [window.title for window in matches[:10]],
                    "query": arguments.window,
                    "action": arguments.action,
                },
            )
        operations = {
            "focus": self.backend.focus_window,
            "minimize": self.backend.minimize_window,
            "maximize": self.backend.maximize_window,
            "restore": self.backend.restore_window,
        }
        selected = matches if arguments.all_matches else matches[:1]

        def close_with_focus_recovery(window: WindowInfo) -> bool:
            # Most applications accept WM_CLOSE in the background. Some packaged Windows apps
            # (notably Calculator on some builds) do not process it until their top-level window
            # is activated. Try the normal non-disruptive close first, then focus and retry only
            # when Windows did not verify closure.
            application = (window.application or "").casefold()
            quick_timeout = (
                0.75
                if application
                in {
                    "calculatorapp.exe",
                    "applicationframehost.exe",
                }
                else 3.0
            )
            if self.backend.close_window(window.handle, quick_timeout):
                return True

            still_open = next(
                (item for item in self.backend.list_windows() if item.handle == window.handle),
                None,
            )
            if still_open is None:
                return True
            try:
                if still_open.minimized:
                    self.backend.restore_window(still_open.handle)
                self.backend.focus_window(still_open.handle)
            except ToolExecutionError:
                # A second WM_CLOSE can still succeed even if Windows refuses foreground access.
                pass

            still_open = next(
                (item for item in self.backend.list_windows() if item.handle == window.handle),
                None,
            )
            if still_open is None:
                return True
            return self.backend.close_window(still_open.handle, 3.0)

        if arguments.action == "close":
            outcomes = [close_with_focus_recovery(window) for window in selected]
        else:
            outcomes = [operations[arguments.action](window.handle) for window in selected]
        verified = all(outcomes)
        window = selected[0]
        updated = next(
            (item for item in self.backend.list_windows() if item.handle == window.handle), None
        )
        return WindowActionResult(
            window_handle=window.handle,
            window_handles=[item.handle for item in selected],
            target=arguments.window,
            verified=verified,
            window=updated,
            evidence=_evidence(
                "verified" if verified else "not_verified",
                f"named_window_{arguments.action}",
                {
                    "title": window.title,
                    "titles": [item.title for item in selected],
                    "window_handle": window.handle,
                    "window_handles": [item.handle for item in selected],
                },
            ),
            warnings=[] if verified else [f"The window was not confirmed {arguments.action}d."],
        )
