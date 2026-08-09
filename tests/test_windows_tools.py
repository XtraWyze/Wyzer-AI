import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from tests.fake_windows import FakeWindowsBackend
from wyzer.desktop.windows_backend import CtypesWindowsBackend
from wyzer.models import ProcessInfo, ToolResult, VerificationStatus, WindowInfo
from wyzer.state import WorldStateManager
from wyzer.tools import create_default_registry
from wyzer.workers import InProcessExecutor


def execute(tool: str, arguments: dict[str, object], backend: FakeWindowsBackend) -> ToolResult:
    registry = create_default_registry(backend)
    return asyncio.run(InProcessExecutor(registry).execute(tool, arguments, uuid4(), uuid4()))


def test_default_registry_contains_all_builtin_tools() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    expected = {
        "get_system_profile",
        "diagnose_system",
        "open_application",
        "search_installed_applications",
        "list_installed_applications",
        "refresh_application_index",
        "list_installed_games",
        "open_file",
        "control_media",
        "get_current_media",
        "control_master_audio",
        "list_audio_sessions",
        "control_application_audio",
        "mute_all_audio_except",
        "list_running_processes",
        "is_process_running",
        "wait_ms",
        "get_foreground_window",
        "list_open_windows",
        "move_named_window_to_monitor",
        "get_monitor_layout",
        "control_named_window",
        "search_files",
        "list_directory",
        "create_directory",
        "copy_path",
        "move_path",
        "rename_path",
        "delete_path",
        "open_indexed_folder",
        "read_text_file",
        "refresh_file_index",
        "browser_start",
        "browser_stop",
        "browser_status",
        "browser_open_url",
        "browser_search_web",
        "browser_inspect_page",
        "browser_click",
        "browser_type_text",
        "browser_press_key",
        "browser_scroll",
        "browser_history",
        "browser_list_tabs",
        "browser_switch_tab",
        "browser_close_tab",
        "read_clipboard",
        "write_clipboard",
        "copy_selected_text",
        "paste_clipboard",
        "inspect_desktop_ui",
        "click_desktop_element",
        "type_desktop_text",
        "press_desktop_key",
        "inspect_screen",
        "activate_visual_target",
    }
    assert set(registry) == expected


def test_desktop_application_resolution_checks_standard_install_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr("wyzer.desktop.windows_backend.shutil.which", lambda _: None)

    command, executable = CtypesWindowsBackend._application_command("Chrome")

    assert command == [str(chrome), "--new-window", "about:blank"]
    assert executable == "chrome.exe"


def test_packaged_application_resolution_uses_apps_folder() -> None:
    command, executable = CtypesWindowsBackend._application_command("Xbox")

    assert command[0] == "explorer.exe"
    assert command[1].startswith("shell:AppsFolder\\Microsoft.GamingApp_")
    assert executable == "Xbox"


def test_tray_application_resolution_uses_activation_protocol() -> None:
    spotify_command, spotify_executable = CtypesWindowsBackend._application_command("Spotify")
    discord_command, discord_executable = CtypesWindowsBackend._application_command("Discord")

    assert spotify_command == ["explorer.exe", "spotify:"]
    assert spotify_executable == "Spotify"
    assert discord_command == ["explorer.exe", "discord:"]
    assert discord_executable == "Discord"


def test_open_application_returns_verified_evidence() -> None:
    result = execute("open_application", {"application": "Calculator"}, FakeWindowsBackend())
    assert result.ok is True
    assert result.evidence["verification_status"] == VerificationStatus.VERIFIED
    assert result.data is not None
    assert result.data["window"]["title"] == "Calculator"


def test_unverified_launch_is_not_reported_as_verified() -> None:
    backend = FakeWindowsBackend()
    backend.verify_actions = False
    result = execute("open_application", {"application": "Calculator"}, backend)
    assert result.ok is True
    assert result.evidence["verification_status"] == VerificationStatus.NOT_VERIFIED
    assert result.warnings


def test_expected_windows_error_remains_structured() -> None:
    result = execute("open_application", {"application": "missing"}, FakeWindowsBackend())
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "APPLICATION_NOT_FOUND"


def test_open_application_restores_and_focuses_existing_window() -> None:
    backend = FakeWindowsBackend()
    backend.windows.append(
        WindowInfo(
            handle=101,
            title="Friends - Discord",
            process_id=25,
            application="Discord.exe",
            minimized=True,
            monitor_id="monitor:2",
        )
    )

    result = execute("open_application", {"application": "Discord"}, backend)

    assert result.ok is True
    assert result.data is not None
    assert result.data["outcome"] == "focused"
    assert result.data["window"]["handle"] == 101
    assert result.data["window"]["minimized"] is False
    assert backend.foreground_handle == 101
    assert not any(process.name == "Discord.exe" for process in backend.processes)


def test_open_application_launches_then_focuses_missing_app() -> None:
    backend = FakeWindowsBackend()

    result = execute("open_application", {"application": "Spotify"}, backend)

    assert result.ok is True
    assert result.evidence["verification_status"] == VerificationStatus.VERIFIED
    assert result.data is not None
    assert result.data["outcome"] == "opened"
    assert result.data["window"]["title"] == "Spotify"
    assert backend.foreground_handle == result.data["window"]["handle"]


def test_open_application_does_not_claim_foreground_without_a_window() -> None:
    backend = FakeWindowsBackend()
    backend.verify_actions = False

    result = execute("open_application", {"application": "Spotify"}, backend)

    assert result.ok is True
    assert result.evidence["verification_status"] == VerificationStatus.NOT_VERIFIED
    assert result.data is not None
    assert result.data["window"] is None
    assert result.warnings


def test_monitor_move_schema_uses_structured_destination() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    definition = registry.get("move_named_window_to_monitor").definition()
    properties = definition.arguments_schema["properties"]

    assert "destination" in properties
    assert "monitor" not in properties


def test_other_monitor_relation_moves_and_reports_friendly_label() -> None:
    backend = FakeWindowsBackend()

    result = execute(
        "move_named_window_to_monitor",
        {"window": "Notes", "destination": {"relation": "other"}},
        backend,
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["changed"] is True
    assert result.data["source_label"] == "monitor 1"
    assert result.data["destination_label"] == "monitor 2"
    assert result.data["window"]["monitor_id"] == "monitor:2"


def test_raw_monitor_handle_cannot_impersonate_display_device() -> None:
    result = execute(
        "move_named_window_to_monitor",
        {"window": "Notes", "destination": {"device_name": "monitor:1"}},
        FakeWindowsBackend(),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "MONITOR_NOT_FOUND"


def test_named_window_control_resolves_title_without_model_guessing_handle() -> None:
    backend = FakeWindowsBackend()
    result = execute("control_named_window", {"window": "Notes", "action": "minimize"}, backend)
    assert result.ok is True
    assert result.data is not None
    assert result.data["window_handle"] == 100


def test_named_calculator_ignores_application_frame_host_shadow() -> None:
    backend = FakeWindowsBackend()
    backend.windows = [
        WindowInfo(
            handle=200,
            title="Calculator",
            process_id=20,
            application="CalculatorApp.exe",
            monitor_id="monitor:1",
            minimized=True,
        ),
        WindowInfo(
            handle=201,
            title="Calculator",
            process_id=21,
            application="ApplicationFrameHost.exe",
            monitor_id="monitor:1",
            minimized=True,
        ),
    ]

    result = execute(
        "control_named_window",
        {"window": "Calculator", "action": "restore"},
        backend,
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["window_handle"] == 200
    assert next(window for window in backend.windows if window.handle == 200).minimized is False


def test_named_calculator_ignores_explorer_filename_title_collision() -> None:
    backend = FakeWindowsBackend()
    backend.windows = [
        WindowInfo(
            handle=200,
            title="Calculator",
            process_id=20,
            application="CalculatorApp.exe",
            monitor_id="monitor:1",
            minimized=False,
        ),
        WindowInfo(
            handle=201,
            title="WyzerNext-Calculator-Wrapper-Fix-Patch-Only.zip",
            process_id=21,
            application="explorer.exe",
            monitor_id="monitor:1",
            minimized=False,
        ),
    ]

    result = execute(
        "control_named_window",
        {"window": "Calculator", "action": "minimize"},
        backend,
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["window_handle"] == 200
    assert next(window for window in backend.windows if window.handle == 200).minimized is True
    assert next(window for window in backend.windows if window.handle == 201).minimized is False


def test_calculator_query_does_not_match_explorer_filename_without_calculator() -> None:
    backend = FakeWindowsBackend()
    backend.windows = [
        WindowInfo(
            handle=201,
            title="WyzerNext-Calculator-Wrapper-Fix-Patch-Only.zip",
            process_id=21,
            application="explorer.exe",
            monitor_id="monitor:1",
        )
    ]

    result = execute(
        "control_named_window",
        {"window": "Calculator", "action": "minimize"},
        backend,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "WINDOW_NOT_FOUND"


def test_calculator_filter_preserves_two_real_windows_as_ambiguous() -> None:
    backend = FakeWindowsBackend()
    backend.windows = [
        WindowInfo(
            handle=200,
            title="Calculator",
            process_id=20,
            application="CalculatorApp.exe",
            monitor_id="monitor:1",
        ),
        WindowInfo(
            handle=201,
            title="Calculator",
            process_id=21,
            application="CalculatorApp.exe",
            monitor_id="monitor:1",
        ),
    ]

    result = execute(
        "control_named_window",
        {"window": "Calculator", "action": "restore"},
        backend,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "AMBIGUOUS_WINDOW"


def test_live_calculator_query_hides_application_frame_host_shadow() -> None:
    backend = FakeWindowsBackend()
    backend.windows = [
        WindowInfo(
            handle=200,
            title="Calculator",
            process_id=20,
            application="CalculatorApp.exe",
            monitor_id="monitor:1",
            minimized=True,
        ),
        WindowInfo(
            handle=201,
            title="Calculator",
            process_id=21,
            application="ApplicationFrameHost.exe",
            monitor_id="monitor:1",
            minimized=True,
        ),
    ]

    result = execute("list_open_windows", {"query": "Calculator"}, backend)

    assert result.ok is True
    assert result.data is not None
    assert result.data["count"] == 1
    assert result.data["windows"][0]["handle"] == 200


def test_named_window_control_resolves_workspace_name_to_foreground_window() -> None:
    backend = FakeWindowsBackend()
    backend.windows[0] = backend.windows[0].model_copy(update={"application": "pwsh.exe"})
    result = execute(
        "control_named_window",
        {"window": Path.cwd().name, "action": "minimize"},
        backend,
    )
    assert result.ok is True
    assert result.data is not None
    assert result.data["window_handle"] == 100


def test_gui_processes_are_spawned_without_inheriting_console_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 42

    def fake_popen(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr("wyzer.desktop.windows_backend.subprocess.Popen", fake_popen)
    process = CtypesWindowsBackend._spawn_silently(["example.exe"])

    assert process.pid == 42
    assert captured["stdout"] == -3
    assert captured["stderr"] == -3
    assert captured["stdin"] == -3


def test_world_state_applies_only_successful_typed_observations() -> None:
    backend = FakeWindowsBackend()
    manager = WorldStateManager()
    foreground = execute("get_foreground_window", {}, backend)
    windows = execute("list_open_windows", {}, backend)
    monitors = execute("get_monitor_layout", {}, backend)
    for result in [foreground, windows, monitors]:
        manager.apply_tool_observation(result)
    snapshot = manager.snapshot()
    assert snapshot.foreground_window is not None
    assert snapshot.foreground_window.title == "Notes"
    assert len(snapshot.known_open_windows) == 1
    assert len(snapshot.monitor_layout) == 2


def test_process_query_requires_exactly_one_selector() -> None:
    result = execute("is_process_running", {}, FakeWindowsBackend())
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "INVALID_TOOL_ARGUMENTS"


def test_friendly_application_status_counts_a_minimized_window_as_open() -> None:
    backend = FakeWindowsBackend()
    backend.windows.append(
        WindowInfo(
            handle=101,
            title="Calculator",
            process_id=25,
            application="CalculatorApp.exe",
            minimized=True,
            monitor_id="monitor:1",
        )
    )
    backend.processes.append(ProcessInfo(process_id=25, name="CalculatorApp.exe"))

    result = execute("is_process_running", {"name": "Calculator"}, backend)

    assert result.ok is True
    assert result.data is not None
    assert result.data["running"] is True
    assert result.data["status"] == "minimized"
    assert result.data["windows"][0]["handle"] == 101
    assert result.data["matched_processes"][0]["name"] == "CalculatorApp.exe"


def test_list_open_windows_can_live_filter_by_friendly_name() -> None:
    backend = FakeWindowsBackend()
    backend.windows.append(
        WindowInfo(
            handle=101,
            title="Calculator",
            process_id=25,
            application="CalculatorApp.exe",
            minimized=True,
            monitor_id="monitor:1",
        )
    )

    result = execute("list_open_windows", {"query": "Calculator"}, backend)

    assert result.ok is True
    assert result.data is not None
    assert result.data["query"] == "Calculator"
    assert result.data["count"] == 1
    assert result.data["windows"][0]["minimized"] is True


def test_open_application_waits_briefly_for_delayed_window_identity() -> None:
    class DelayedWindowBackend(FakeWindowsBackend):
        def __init__(self) -> None:
            super().__init__()
            self.verification_timeout_seconds = 0.25
            self._pending_window: WindowInfo | None = None
            self._window_polls = 0

        def launch_application(self, application: str) -> tuple[int | None, str]:
            process_id = 20
            self.processes.append(ProcessInfo(process_id=process_id, name=f"{application}.exe"))
            self._pending_window = WindowInfo(
                handle=201,
                title=application,
                process_id=process_id,
                application=f"{application}.exe",
                monitor_id="monitor:1",
            )
            return process_id, f"{application}.exe"

        def list_windows(self) -> list[WindowInfo]:
            self._window_polls += 1
            if self._pending_window is not None and self._window_polls >= 3:
                self.windows.append(self._pending_window)
                self._pending_window = None
            return super().list_windows()

    result = execute("open_application", {"application": "Calculator"}, DelayedWindowBackend())

    assert result.ok is True
    assert result.data is not None
    assert result.data["window"]["title"] == "Calculator"


def test_open_calculator_ignores_explorer_title_collision_during_launch() -> None:
    backend = FakeWindowsBackend()
    backend.windows.append(
        WindowInfo(
            handle=150,
            title="WyzerNext-Calculator-Title-Collision-Fix-Patch-Only.zip",
            process_id=10,
            application="explorer.exe",
            monitor_id="monitor:1",
        )
    )

    result = execute("open_application", {"application": "Calculator"}, backend)

    assert result.ok is True
    assert result.data is not None
    window = result.data["window"]
    assert window is not None
    assert window["application"].casefold() == "calculator.exe"
    assert window["title"] == "Calculator"


def test_named_window_close_focuses_and_retries_when_background_close_is_not_verified() -> None:
    class FocusRequiredCloseBackend(FakeWindowsBackend):
        def __init__(self) -> None:
            super().__init__()
            self.windows = [
                WindowInfo(
                    handle=200,
                    title="Calculator",
                    process_id=20,
                    application="CalculatorApp.exe",
                    monitor_id="monitor:1",
                )
            ]
            self.foreground_handle = 1000
            self.close_attempts = 0

        def close_window(self, handle: int, timeout_seconds: float = 3) -> bool:
            del timeout_seconds
            self.close_attempts += 1
            if self.foreground_handle != handle:
                return False
            self.windows = [window for window in self.windows if window.handle != handle]
            return True

    backend = FocusRequiredCloseBackend()

    result = execute(
        "control_named_window",
        {"window": "Calculator", "action": "close"},
        backend,
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["verified"] is True
    assert result.data["target"] == "Calculator"
    assert backend.close_attempts == 2
    assert backend.windows == []


def test_diagnose_system_is_read_only_and_returns_structured_telemetry() -> None:
    backend = FakeWindowsBackend()

    result = execute("diagnose_system", {"scope": "performance"}, backend)

    assert result.ok is True
    assert result.data is not None
    assert result.data["scope"] == "performance"
    assert result.data["health"] == "attention"
    assert result.data["telemetry"]["performance"]["cpu_percent"] == 42.0
    assert result.data["findings"][0]["component"] == "event_log"


def test_diagnose_system_schema_keeps_scope_bounded() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    schema = registry.get("diagnose_system").definition().arguments_schema

    assert schema["properties"]["scope"]["enum"] == [
        "auto",
        "performance",
        "hardware",
        "storage",
        "network",
        "windows",
        "security",
    ]
    assert registry.get("diagnose_system").read_only is True


def test_native_tool_menu_hides_internal_diagnostics() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    visible = {tool.function.name for tool in registry.native_tools()}

    assert "open_application" in visible
    assert "control_named_window" in visible
    assert "move_named_window_to_monitor" in visible
    assert "mute_all_audio_except" in visible
    assert "get_system_profile" in visible
    assert "diagnose_system" in visible
    assert "list_running_processes" not in visible
    assert "is_process_running" in visible
    assert "refresh_application_index" not in visible
    assert "list_installed_applications" not in visible
    assert "wait_ms" not in visible
    assert "inspect_desktop_ui" not in visible
    assert "click_desktop_element" not in visible
    assert registry.get("inspect_desktop_ui", require_available=False).llm_visible is False
    assert registry.get("click_desktop_element", require_available=False).llm_visible is False


def test_model_visible_action_schemas_use_enums_and_flat_monitor_destination() -> None:
    registry = create_default_registry(FakeWindowsBackend())

    window_schema = registry.get("control_named_window").definition().arguments_schema
    audio_schema = registry.get("control_master_audio").definition().arguments_schema
    monitor_schema = registry.get("move_named_window_to_monitor").definition().arguments_schema

    assert window_schema["properties"]["action"]["enum"] == [
        "focus",
        "minimize",
        "maximize",
        "restore",
        "close",
    ]
    assert audio_schema["properties"]["operation"]["enum"] == [
        "increase",
        "decrease",
        "set",
        "mute",
        "unmute",
        "toggle_mute",
        "get",
    ]
    assert monitor_schema["properties"]["destination"]["type"] == "string"
    assert "$ref" not in monitor_schema["properties"]["destination"]
