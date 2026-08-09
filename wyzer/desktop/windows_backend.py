"""Concrete Win32 backend for processes, windows, monitors, and launching."""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import psutil

from wyzer.desktop.application_index import WindowsApplicationIndex
from wyzer.desktop.audio import AudioMixer, PycawCoreAudio
from wyzer.models import (
    MonitorDestination,
    MonitorInfo,
    ProcessInfo,
    Rect,
    WindowInfo,
    WindowMoveOutcome,
)
from wyzer.tools.base import ToolExecutionError

WM_CLOSE = 0x0010
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_SHOW = 5
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class CtypesWindowsBackend:
    _APPLICATION_ALIASES: ClassVar[dict[str, str]] = {
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "notepad": "notepad.exe",
        "file explorer": "explorer.exe",
        "explorer": "explorer.exe",
        "paint": "mspaint.exe",
        "command prompt": "cmd.exe",
    }
    _PACKAGED_APPLICATIONS: ClassVar[dict[str, str]] = {
        "xbox": "Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App",
        "xbox app": "Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App",
    }
    _URI_APPLICATIONS: ClassVar[dict[str, str]] = {
        "discord": "discord:",
        "spotify": "spotify:",
    }
    _DESKTOP_APPLICATION_PATHS: ClassVar[dict[str, tuple[str, ...]]] = {
        "chrome": ("Google/Chrome/Application/chrome.exe",),
        "google chrome": ("Google/Chrome/Application/chrome.exe",),
        "edge": ("Microsoft/Edge/Application/msedge.exe",),
        "microsoft edge": ("Microsoft/Edge/Application/msedge.exe",),
        "firefox": ("Mozilla Firefox/firefox.exe",),
        "battle.net": ("Battle.net/Battle.net.exe",),
        "battlenet": ("Battle.net/Battle.net.exe",),
        "ea app": ("Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe",),
    }

    def __init__(
        self,
        *,
        audio_options: dict[str, object] | None = None,
    ) -> None:
        if platform.system() != "Windows":
            raise ToolExecutionError("WINDOWS_REQUIRED", "This tool requires Windows.")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._applications = WindowsApplicationIndex()
        self._previous_monitor_by_window: dict[int, str] = {}
        audio_options = audio_options or {}
        self.audio_timeout_seconds = _audio_float_option(
            audio_options, "core_audio_timeout_seconds", 5
        )
        self._audio = AudioMixer(
            PycawCoreAudio(),
            master_step=_audio_int_option(audio_options, "default_master_volume_step", 10),
            application_step=_audio_int_option(
                audio_options, "default_application_volume_step", 10
            ),
            match_threshold=_audio_float_option(
                audio_options, "application_audio_match_threshold", 0.72
            ),
            ambiguity_margin=_audio_float_option(
                audio_options, "application_audio_ambiguity_margin", 0.08
            ),
            control_all_matching_sessions=_audio_bool_option(
                audio_options, "control_all_matching_sessions", True
            ),
            core_audio_timeout_seconds=self.audio_timeout_seconds,
            fallback=self.control_volume,
        )
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self._user32.MonitorFromWindow.restype = wintypes.HANDLE
        self._user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
        self._user32.GetMonitorInfoW.restype = wintypes.BOOL
        self._user32.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(DISPLAY_DEVICEW),
            wintypes.DWORD,
        ]
        self._user32.EnumDisplayDevicesW.restype = wintypes.BOOL
        self._user32.MoveWindow.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        ]
        self._user32.MoveWindow.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.IsZoomed.argtypes = [wintypes.HWND]
        self._user32.IsZoomed.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self._user32.BringWindowToTop.restype = wintypes.BOOL
        self._user32.SetActiveWindow.argtypes = [wintypes.HWND]
        self._user32.SetActiveWindow.restype = wintypes.HWND
        self._user32.SetFocus.argtypes = [wintypes.HWND]
        self._user32.SetFocus.restype = wintypes.HWND
        self._user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self._user32.AttachThreadInput.restype = wintypes.BOOL
        self._user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self._user32.SetWindowPos.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def list_processes(self) -> list[ProcessInfo]:
        processes: list[ProcessInfo] = []
        for process in psutil.process_iter(["pid", "name", "exe", "username"]):
            try:
                info = process.info
                processes.append(
                    ProcessInfo(
                        process_id=int(info["pid"]),
                        name=str(info.get("name") or ""),
                        executable=_optional_string(info.get("exe")),
                        username=_optional_string(info.get("username")),
                    )
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return sorted(processes, key=lambda item: (item.name.casefold(), item.process_id))

    def is_process_running(self, *, process_id: int | None = None, name: str | None = None) -> bool:
        if process_id is not None:
            return bool(psutil.pid_exists(process_id))
        expected = (name or "").casefold().removesuffix(".exe")
        return any(
            process.name.casefold().removesuffix(".exe") == expected
            for process in self.list_processes()
        )

    def launch_application(self, application: str) -> tuple[int | None, str]:
        requested = application.strip()
        normalized = requested.casefold()
        # Built-in Windows aliases and explicitly supported desktop applications are
        # authoritative.  In particular, a fuzzy Start Menu match for "Notepad++"
        # must never replace the exact built-in "Notepad" application.
        known_application = normalized in {
            *self._APPLICATION_ALIASES,
            *self._PACKAGED_APPLICATIONS,
            *self._DESKTOP_APPLICATION_PATHS,
            *self._URI_APPLICATIONS,
        }
        indexed = None if known_application else self._applications.resolve(requested)
        candidates = [] if known_application else self._applications.search(requested, 5)
        if indexed is not None:
            try:
                if indexed.target.startswith("battlenet-product:"):
                    product = indexed.target.partition(":")[2]
                    command, _ = self._application_command("Battle.net")
                    self._spawn_silently([*command, f"--exec=launch {product}"])
                elif indexed.target.startswith(
                    "shell:AppsFolder\\"
                ) or indexed.target.casefold().endswith(".lnk"):
                    self._spawn_silently(["explorer.exe", indexed.target])
                else:
                    os.startfile(indexed.target)
            except OSError as error:
                raise ToolExecutionError(
                    "LAUNCH_FAILED",
                    f"Windows could not open {indexed.name} from the {indexed.source} index.",
                ) from error
            return None, indexed.name
        command, executable = self._application_command(requested)
        try:
            process = self._spawn_silently(command)
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "APPLICATION_NOT_FOUND",
                "No confident installed-application match was found.",
                details={
                    "application": requested,
                    "candidates": [item.name for item in candidates],
                },
            ) from error
        except OSError as error:
            code = (
                "ELEVATION_REQUIRED" if getattr(error, "winerror", None) == 740 else "LAUNCH_FAILED"
            )
            raise ToolExecutionError(code, f"Windows could not open {requested}.") from error
        return process.pid, executable

    @staticmethod
    def _cim_names(class_name: str) -> list[str]:
        """Read a short list of hardware names without opening a console window."""
        if os.name != "nt":
            return []
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                f"Get-CimInstance {class_name} | "
                "ForEach-Object { $_.Name } | "
                "Where-Object { $_ -and $_.Trim() } | "
                "ConvertTo-Json -Compress"
            ),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        import json

        try:
            decoded = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return []
        values = decoded if isinstance(decoded, list) else [decoded]
        names: list[str] = []
        for value in values:
            if isinstance(value, str) and value.strip() and value.strip() not in names:
                names.append(value.strip())
            if len(names) >= 8:
                break
        return names

    def system_profile(self) -> dict[str, Any]:
        """Return a bounded, non-sensitive snapshot of this Windows computer."""
        memory = psutil.virtual_memory()
        windows_version = getattr(sys, "getwindowsversion", lambda: None)()
        build = getattr(windows_version, "build", None)
        operating_system = platform.platform()
        if isinstance(build, int):
            release = "11" if build >= 22_000 else platform.release()
            operating_system = f"Windows {release} (build {build})"
        drives: list[dict[str, object]] = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except (OSError, PermissionError):
                continue
            drives.append(
                {
                    "mountpoint": partition.mountpoint,
                    "file_system": partition.fstype,
                    "total_bytes": usage.total,
                    "free_bytes": usage.free,
                }
            )
            if len(drives) >= 20:
                break
        processor_names = self._cim_names("Win32_Processor")
        processor = processor_names[0] if processor_names else (platform.processor() or None)
        graphics_adapters = self._cim_names("Win32_VideoController")
        return {
            "computer_name": platform.node(),
            "operating_system": operating_system,
            "architecture": platform.machine(),
            "processor": processor,
            "physical_cpu_cores": psutil.cpu_count(logical=False),
            "logical_cpu_cores": psutil.cpu_count(logical=True),
            "graphics_adapters": graphics_adapters,
            "memory_total_bytes": memory.total,
            "memory_available_bytes": memory.available,
            "drives": drives,
        }

    def diagnose_system(self, *, scope: str = "auto") -> dict[str, Any]:
        """Collect a bounded read-only diagnostic snapshot for model reasoning."""
        allowed = {
            "auto",
            "performance",
            "hardware",
            "storage",
            "network",
            "windows",
            "security",
        }
        if scope not in allowed:
            raise ToolExecutionError(
                "INVALID_DIAGNOSTIC_SCOPE",
                f"Unsupported diagnostic scope: {scope}",
                details={"allowed": sorted(allowed)},
            )

        telemetry: dict[str, Any] = {}
        unavailable: list[str] = []
        warnings: list[str] = []
        findings: list[dict[str, Any]] = []

        def include(name: str) -> bool:
            return scope == "auto" or scope == name

        if include("performance"):
            telemetry["performance"] = self._performance_telemetry()
        if include("hardware"):
            hardware, missing = self._hardware_telemetry()
            telemetry["hardware"] = hardware
            unavailable.extend(missing)
        if include("storage"):
            storage, missing = self._storage_telemetry()
            telemetry["storage"] = storage
            unavailable.extend(missing)
        if include("network"):
            network, missing = self._network_telemetry(probe_internet=scope == "network")
            telemetry["network"] = network
            unavailable.extend(missing)
        if include("windows"):
            windows, missing = self._windows_health_telemetry()
            telemetry["windows"] = windows
            unavailable.extend(missing)
        if include("security"):
            security, missing = self._security_telemetry()
            telemetry["security"] = security
            unavailable.extend(missing)

        self._append_diagnostic_findings(telemetry, findings)
        severities = {str(item.get("severity")) for item in findings}
        if "warning" in severities:
            health = "warning"
        elif "attention" in severities:
            health = "attention"
        elif telemetry:
            health = "ok"
        else:
            health = "unknown"

        summary = self._diagnostic_summary(telemetry, findings)
        return {
            "scope": scope,
            "health": health,
            "collected_at": datetime.now(UTC).isoformat(),
            "summary": summary,
            "findings": findings[:24],
            "telemetry": telemetry,
            "unavailable": sorted(set(unavailable))[:20],
            "warnings": warnings,
            "evidence": {"collector": "windows_read_only_diagnostics", "scope": scope},
        }

    @staticmethod
    def _performance_telemetry() -> dict[str, Any]:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk_before = psutil.disk_io_counters()
        net_before = psutil.net_io_counters()
        psutil.cpu_percent(interval=None)

        sampled_processes: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                process.cpu_percent(interval=None)
                sampled_processes.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue

        started = time.monotonic()
        time.sleep(0.20)
        elapsed = max(0.001, time.monotonic() - started)
        cpu_percent = float(psutil.cpu_percent(interval=None))
        disk_after = psutil.disk_io_counters()
        net_after = psutil.net_io_counters()

        top: list[dict[str, Any]] = []
        for process in sampled_processes:
            try:
                info = process.as_dict(attrs=["pid", "name", "memory_info"])
                rss = getattr(info.get("memory_info"), "rss", 0) or 0
                top.append(
                    {
                        "process_id": int(info["pid"]),
                        "name": str(info.get("name") or ""),
                        "cpu_percent": round(float(process.cpu_percent(interval=None)), 1),
                        "memory_bytes": int(rss),
                    }
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        top.sort(key=lambda item: (item["cpu_percent"], item["memory_bytes"]), reverse=True)

        def rate(after: object | None, before: object | None, field: str) -> int | None:
            if after is None or before is None:
                return None
            return max(0, int((getattr(after, field) - getattr(before, field)) / elapsed))

        frequency = psutil.cpu_freq()
        return {
            "cpu_percent": round(cpu_percent, 1),
            "cpu_frequency_mhz": (
                round(float(frequency.current), 1) if frequency is not None else None
            ),
            "memory": {
                "total_bytes": int(memory.total),
                "available_bytes": int(memory.available),
                "used_percent": round(float(memory.percent), 1),
            },
            "swap": {
                "total_bytes": int(swap.total),
                "used_bytes": int(swap.used),
                "used_percent": round(float(swap.percent), 1),
            },
            "disk_io_bytes_per_second": {
                "read": rate(disk_after, disk_before, "read_bytes"),
                "write": rate(disk_after, disk_before, "write_bytes"),
            },
            "network_io_bytes_per_second": {
                "sent": rate(net_after, net_before, "bytes_sent"),
                "received": rate(net_after, net_before, "bytes_recv"),
            },
            "top_processes": top[:8],
            "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
        }

    def _hardware_telemetry(self) -> tuple[dict[str, Any], list[str]]:
        profile = self.system_profile()
        missing: list[str] = []
        hardware: dict[str, Any] = {
            "processor": profile.get("processor"),
            "physical_cpu_cores": profile.get("physical_cpu_cores"),
            "logical_cpu_cores": profile.get("logical_cpu_cores"),
            "memory_total_bytes": profile.get("memory_total_bytes"),
            "graphics_adapters": profile.get("graphics_adapters", []),
        }

        battery = psutil.sensors_battery()
        if battery is not None:
            hardware["battery"] = {
                "percent": round(float(battery.percent), 1),
                "power_plugged": bool(battery.power_plugged),
                "seconds_left": int(battery.secsleft)
                if isinstance(battery.secsleft, (int, float)) and battery.secsleft >= 0
                else None,
            }

        gpus, gpu_missing = self._gpu_telemetry()
        if gpus:
            hardware["gpus"] = gpus
        missing.extend(gpu_missing)

        firmware = self._powershell_json(
            "$b=Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue | "
            "Select-Object -First 1 Manufacturer,SMBIOSBIOSVersion,ReleaseDate;"
            "$m=Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | "
            "Select-Object -First 1 Manufacturer,Product,Version;"
            "[pscustomobject]@{bios=$b;baseboard=$m}|ConvertTo-Json -Depth 4 -Compress",
            timeout=4.0,
        )
        if isinstance(firmware, dict):
            hardware["firmware"] = firmware
        else:
            missing.append("BIOS/baseboard CIM telemetry")
        return hardware, missing

    def _storage_telemetry(self) -> tuple[dict[str, Any], list[str]]:
        volumes: list[dict[str, Any]] = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except (OSError, PermissionError):
                continue
            volumes.append(
                {
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "file_system": partition.fstype,
                    "total_bytes": int(usage.total),
                    "used_bytes": int(usage.used),
                    "free_bytes": int(usage.free),
                    "used_percent": round(float(usage.percent), 1),
                }
            )
            if len(volumes) >= 20:
                break

        missing: list[str] = []
        physical = self._powershell_json(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Select-Object FriendlyName,MediaType,HealthStatus,OperationalStatus,Size | "
            "ConvertTo-Json -Depth 4 -Compress",
            timeout=4.0,
        )
        disks = self._json_list(physical)
        if not disks:
            missing.append("physical disk health (Get-PhysicalDisk unavailable or empty)")
        return {"volumes": volumes, "physical_disks": disks[:16]}, missing

    @staticmethod
    def _network_telemetry(*, probe_internet: bool) -> tuple[dict[str, Any], list[str]]:
        adapters: list[dict[str, Any]] = []
        stats = psutil.net_if_stats()
        addresses = psutil.net_if_addrs()
        for name in sorted(set(stats) | set(addresses), key=str.casefold):
            stat = stats.get(name)
            address_items: list[str] = []
            for address in addresses.get(name, ()):
                family = getattr(address.family, "name", str(address.family))
                if family in {"AF_INET", "AF_INET6"} and address.address:
                    address_items.append(address.address.split("%", 1)[0])
            adapters.append(
                {
                    "name": name,
                    "up": bool(stat.isup) if stat is not None else None,
                    "speed_mbps": int(stat.speed) if stat is not None and stat.speed >= 0 else None,
                    "addresses": address_items[:6],
                }
            )
            if len(adapters) >= 24:
                break

        result: dict[str, Any] = {"adapters": adapters}
        missing: list[str] = []
        if probe_internet:
            dns_ok = False
            tcp_ok = False
            try:
                socket.getaddrinfo("www.microsoft.com", 443, type=socket.SOCK_STREAM)
                dns_ok = True
            except OSError:
                pass
            try:
                with socket.create_connection(("1.1.1.1", 443), timeout=1.5):
                    tcp_ok = True
            except OSError:
                pass
            result["connectivity_probe"] = {
                "dns_resolution": dns_ok,
                "outbound_tcp": tcp_ok,
                "probe_target": "1.1.1.1:443",
            }
        return result, missing

    def _windows_health_telemetry(self) -> tuple[dict[str, Any], list[str]]:
        script = (
            "$svc=Get-CimInstance Win32_Service -ErrorAction SilentlyContinue | "
            "Where-Object {$_.StartMode -eq 'Auto' -and $_.State -ne 'Running'} | "
            "Select-Object -First 20 Name,DisplayName,State,StartMode;"
            "$dev=Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | "
            "Where-Object {$_.ConfigManagerErrorCode -ne 0} | "
            "Select-Object -First 20 Name,PNPClass,ConfigManagerErrorCode;"
            "$start=(Get-Date).AddHours(-24);"
            "$evt=Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2;StartTime=$start} "
            "-MaxEvents 16 -ErrorAction SilentlyContinue | "
            "Select-Object TimeCreated,Id,ProviderName,LevelDisplayName,Message;"
            "$reboot=(Test-Path "
            "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\"
            "Auto Update\\RebootRequired') -or "
            "((Get-ItemProperty "
            "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager' "
            "-Name PendingFileRenameOperations -ErrorAction SilentlyContinue) -ne $null);"
            "[pscustomobject]@{stopped_automatic_services=$svc;device_errors=$dev;"
            "serious_system_events=$evt;reboot_pending=$reboot}|ConvertTo-Json -Depth 5 -Compress"
        )
        data = self._powershell_json(script, timeout=7.0)
        if not isinstance(data, dict):
            return {}, ["Windows services/device/event-log telemetry"]
        for key in ("stopped_automatic_services", "device_errors", "serious_system_events"):
            data[key] = self._json_list(data.get(key))
        events = data.get("serious_system_events", [])
        for event in events:
            if isinstance(event, dict) and isinstance(event.get("Message"), str):
                event["Message"] = event["Message"].replace("\r", " ").replace("\n", " ")[:360]
        return data, []

    def _security_telemetry(self) -> tuple[dict[str, Any], list[str]]:
        script = (
            "$mp=Get-MpComputerStatus -ErrorAction SilentlyContinue | "
            "Select-Object AntivirusEnabled,AntispywareEnabled,RealTimeProtectionEnabled,"
            "BehaviorMonitorEnabled,IoavProtectionEnabled,NISEnabled,AntivirusSignatureLastUpdated;"
            "$fw=Get-NetFirewallProfile -ErrorAction SilentlyContinue | "
            "Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction;"
            "[pscustomobject]@{defender=$mp;firewall=$fw}|ConvertTo-Json -Depth 4 -Compress"
        )
        data = self._powershell_json(script, timeout=5.0)
        if not isinstance(data, dict):
            return {}, ["Windows Defender/firewall telemetry"]
        data["firewall"] = self._json_list(data.get("firewall"))
        return data, []

    def _gpu_telemetry(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Return normalized GPU telemetry across NVIDIA and AMD on Windows."""
        missing: list[str] = []
        gpus: list[dict[str, Any]] = []

        nvidia = self._nvidia_gpu_telemetry()
        if nvidia:
            gpus.extend(nvidia)

        windows_gpus = self._windows_gpu_telemetry()
        if windows_gpus:
            existing_names = {str(item.get("name") or "").casefold() for item in gpus}
            for gpu in windows_gpus:
                name = str(gpu.get("name") or "").casefold()
                if name and name in existing_names:
                    continue
                gpus.append(gpu)

        adapters = [str(item) for item in self.system_profile().get("graphics_adapters", [])]
        lowered = [item.casefold() for item in adapters]
        has_nvidia = any("nvidia" in item for item in lowered)
        has_amd = any(
            marker in item
            for item in lowered
            for marker in ("amd", "radeon", "advanced micro devices")
        )
        vendors_found = {str(item.get("vendor") or "").casefold() for item in gpus}
        if has_nvidia and "nvidia" not in vendors_found:
            missing.append("NVIDIA live telemetry (nvidia-smi unavailable)")
        if has_amd and "amd" not in vendors_found:
            missing.append("AMD live telemetry (Windows GPU counters/CIM unavailable)")
        if not gpus and adapters:
            missing.append("Live GPU utilization/VRAM telemetry")
        return gpus, missing

    def _windows_gpu_telemetry(self) -> list[dict[str, Any]] | None:
        """Collect vendor-neutral Windows GPU telemetry, including AMD Radeon.

        Windows GPU performance counters provide live engine utilization and adapter
        memory without a vendor SDK. If more than one non-NVIDIA adapter exists, the
        live counters are kept as aggregate evidence instead of being assigned to the
        wrong physical adapter.
        """
        script = r"""
$controllers = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
    Select-Object Name,DriverVersion,PNPDeviceID,AdapterRAM)

$registryAdapters = @()
$displayClass = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*'
Get-ItemProperty $displayClass -ErrorAction SilentlyContinue | ForEach-Object {
    $mem = $_.'HardwareInformation.qwMemorySize'
    if ($null -eq $mem) { $mem = $_.'HardwareInformation.MemorySize' }
    if ($_.DriverDesc) {
        $memoryOut = $null
        if ($null -ne $mem) { $memoryOut = [uint64]$mem }
        $registryAdapters += [pscustomobject]@{
            Name = [string]$_.DriverDesc
            MemoryBytes = $memoryOut
        }
    }
}

$engineSamples = @()
$memorySamples = @()
try {
    $engineSamples = @((Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples)
} catch {}
try {
    $memorySamples = @((Get-Counter @('\GPU Adapter Memory(*)\Dedicated Usage','\GPU Adapter Memory(*)\Shared Usage') -ErrorAction Stop).CounterSamples)
} catch {}

$engineValues = @($engineSamples | ForEach-Object { [double]$_.CookedValue })
$util = $null
if ($engineValues.Count -gt 0) {
    $util = ($engineValues | Measure-Object -Maximum).Maximum
}
$dedicated = 0.0
$shared = 0.0
$hasDedicated = $false
$hasShared = $false
foreach ($sample in $memorySamples) {
    if ($sample.Path -like '*Dedicated Usage') {
        $dedicated += [double]$sample.CookedValue
        $hasDedicated = $true
    } elseif ($sample.Path -like '*Shared Usage') {
        $shared += [double]$sample.CookedValue
        $hasShared = $true
    }
}

$result = @()
foreach ($controller in $controllers) {
    $name = [string]$controller.Name
    $vendor = 'unknown'
    if ($name -match '(?i)NVIDIA') { $vendor = 'nvidia' }
    elseif ($name -match '(?i)(AMD|Radeon|Advanced Micro Devices)') { $vendor = 'amd' }
    elseif ($name -match '(?i)Intel') { $vendor = 'intel' }

    $memoryBytes = $null
    $regMatch = $registryAdapters | Where-Object {
        $_.Name -eq $name -or $name.Contains($_.Name) -or $_.Name.Contains($name)
    } | Select-Object -First 1
    if ($null -ne $regMatch -and $null -ne $regMatch.MemoryBytes) {
        $memoryBytes = [uint64]$regMatch.MemoryBytes
    } elseif ($null -ne $controller.AdapterRAM) {
        $candidate = [uint64]$controller.AdapterRAM
        # Win32_VideoController.AdapterRAM is uint32 and can truncate at 4 GiB.
        if ($candidate -lt 4294967295) { $memoryBytes = $candidate }
    }

    $result += [pscustomobject]@{
        name = $name
        vendor = $vendor
        driver_version = [string]$controller.DriverVersion
        pnp_device_id = [string]$controller.PNPDeviceID
        memory_total_bytes = $memoryBytes
    }
}

$dedicatedOut = $null
$sharedOut = $null
if ($hasDedicated) { $dedicatedOut = [uint64][math]::Max(0,$dedicated) }
if ($hasShared) { $sharedOut = [uint64][math]::Max(0,$shared) }

[pscustomobject]@{
    adapters = $result
    live = [pscustomobject]@{
        utilization_percent = $util
        dedicated_used_bytes = $dedicatedOut
        shared_used_bytes = $sharedOut
        adapter_count = $result.Count
    }
} | ConvertTo-Json -Depth 5 -Compress
"""
        data = self._powershell_json(script, timeout=5.0)
        if not isinstance(data, dict):
            return None
        adapters = self._json_list(data.get("adapters"))
        raw_live = data.get("live")
        live: dict[str, Any] = raw_live if isinstance(raw_live, dict) else {}
        if not adapters:
            return None

        non_nvidia = [
            item for item in adapters if self._gpu_vendor(str(item.get("name") or "")) != "nvidia"
        ]
        can_map_live = len(non_nvidia) == 1
        output: list[dict[str, Any]] = []
        for item in non_nvidia:
            name = str(item.get("name") or "")
            total_bytes = item.get("memory_total_bytes")
            total_mib = (
                round(float(total_bytes) / (1024 * 1024), 2)
                if isinstance(total_bytes, (int, float)) and total_bytes > 0
                else None
            )
            dedicated_bytes = live.get("dedicated_used_bytes") if can_map_live else None
            used_mib = (
                round(float(dedicated_bytes) / (1024 * 1024), 2)
                if isinstance(dedicated_bytes, (int, float)) and dedicated_bytes >= 0
                else None
            )
            output.append(
                {
                    "name": name,
                    "vendor": self._gpu_vendor(name),
                    "driver_version": item.get("driver_version") or None,
                    "utilization_percent": (
                        self._number(str(live.get("utilization_percent")))
                        if can_map_live and live.get("utilization_percent") is not None
                        else None
                    ),
                    "memory_total_mib": total_mib,
                    "memory_used_mib": used_mib,
                    "memory_free_mib": (
                        round(max(0.0, total_mib - used_mib), 2)
                        if total_mib is not None and used_mib is not None
                        else None
                    ),
                    "temperature_c": None,
                    "power_draw_w": None,
                    "source": "windows_gpu_counters+cim",
                    "pnp_device_id": item.get("pnp_device_id") or None,
                }
            )

        if not output:
            return None
        if len(non_nvidia) > 1:
            aggregate = {
                "utilization_percent": live.get("utilization_percent"),
                "dedicated_used_bytes": live.get("dedicated_used_bytes"),
                "shared_used_bytes": live.get("shared_used_bytes"),
            }
            for item in output:
                item["windows_aggregate_live"] = aggregate
        return output

    @staticmethod
    def _gpu_vendor(name: str) -> str:
        lowered = name.casefold()
        if "nvidia" in lowered:
            return "nvidia"
        if any(marker in lowered for marker in ("amd", "radeon", "advanced micro devices")):
            return "amd"
        if "intel" in lowered:
            return "intel"
        return "unknown"

    @staticmethod
    def _nvidia_gpu_telemetry() -> list[dict[str, Any]] | None:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            common = Path(
                os.environ.get("PROGRAMW6432", r"C:\Program Files"),
                "NVIDIA Corporation",
                "NVSMI",
                "nvidia-smi.exe",
            )
            executable = str(common) if common.is_file() else None
        if executable is None:
            return None
        fields = (
            "name,driver_version,utilization.gpu,memory.total,memory.used,memory.free,"
            "temperature.gpu,power.draw"
        )
        try:
            completed = subprocess.run(
                [
                    executable,
                    f"--query-gpu={fields}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        gpus: list[dict[str, Any]] = []
        for raw_line in completed.stdout.splitlines():
            parts = [part.strip() for part in raw_line.split(",")]
            if len(parts) != 8:
                continue
            gpus.append(
                {
                    "name": parts[0],
                    "vendor": "nvidia",
                    "driver_version": parts[1],
                    "utilization_percent": CtypesWindowsBackend._number(parts[2]),
                    "memory_total_mib": CtypesWindowsBackend._number(parts[3]),
                    "memory_used_mib": CtypesWindowsBackend._number(parts[4]),
                    "memory_free_mib": CtypesWindowsBackend._number(parts[5]),
                    "temperature_c": CtypesWindowsBackend._number(parts[6]),
                    "power_draw_w": CtypesWindowsBackend._number(parts[7]),
                    "source": "nvidia-smi",
                }
            )
        return gpus or None

    @staticmethod
    def _number(value: str) -> float | None:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _json_list(value: object) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _powershell_json(script: str, *, timeout: float) -> object | None:
        executable = (
            shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        )
        if executable is None:
            return None
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = completed.stdout.strip().lstrip("\ufeff")
        if completed.returncode != 0 or not output:
            return None
        try:
            decoded: object = json.loads(output)
            return decoded
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _append_diagnostic_findings(
        telemetry: dict[str, Any], findings: list[dict[str, Any]]
    ) -> None:
        performance = telemetry.get("performance")
        if isinstance(performance, dict):
            cpu = float(performance.get("cpu_percent") or 0)
            memory = performance.get("memory")
            memory_percent = (
                float(memory.get("used_percent") or 0) if isinstance(memory, dict) else 0.0
            )
            if cpu >= 90:
                findings.append(
                    {
                        "severity": "warning",
                        "component": "cpu",
                        "message": "CPU load is currently very high.",
                        "evidence": {"cpu_percent": cpu},
                    }
                )
            elif cpu >= 75:
                findings.append(
                    {
                        "severity": "attention",
                        "component": "cpu",
                        "message": "CPU load is elevated.",
                        "evidence": {"cpu_percent": cpu},
                    }
                )
            if memory_percent >= 90:
                findings.append(
                    {
                        "severity": "warning",
                        "component": "memory",
                        "message": "Memory usage is very high.",
                        "evidence": {"used_percent": memory_percent},
                    }
                )
            elif memory_percent >= 82:
                findings.append(
                    {
                        "severity": "attention",
                        "component": "memory",
                        "message": "Memory usage is elevated.",
                        "evidence": {"used_percent": memory_percent},
                    }
                )

        storage = telemetry.get("storage")
        if isinstance(storage, dict):
            for volume in CtypesWindowsBackend._json_list(storage.get("volumes")):
                used = float(volume.get("used_percent") or 0)
                if used >= 95:
                    findings.append(
                        {
                            "severity": "warning",
                            "component": "storage",
                            "message": (
                                f"Drive {volume.get('mountpoint') or volume.get('device')} "
                                "is almost full."
                            ),
                            "evidence": {"used_percent": used},
                        }
                    )
                elif used >= 90:
                    findings.append(
                        {
                            "severity": "attention",
                            "component": "storage",
                            "message": (
                                f"Drive {volume.get('mountpoint') or volume.get('device')} "
                                "has low free space."
                            ),
                            "evidence": {"used_percent": used},
                        }
                    )
            for disk in CtypesWindowsBackend._json_list(storage.get("physical_disks")):
                health = str(disk.get("HealthStatus") or "").casefold()
                if health and health not in {"healthy", "unknown"}:
                    findings.append(
                        {
                            "severity": "warning",
                            "component": "storage",
                            "message": (
                                f"A physical disk reports health status {disk.get('HealthStatus')}."
                            ),
                            "evidence": {"disk": disk.get("FriendlyName")},
                        }
                    )

        hardware = telemetry.get("hardware")
        if isinstance(hardware, dict):
            for gpu in CtypesWindowsBackend._json_list(hardware.get("gpus")):
                gpu_temperature = gpu.get("temperature_c")
                total = gpu.get("memory_total_mib")
                gpu_used = gpu.get("memory_used_mib")
                if isinstance(gpu_temperature, (int, float)) and gpu_temperature >= 85:
                    findings.append(
                        {
                            "severity": "warning",
                            "component": "gpu",
                            "message": "GPU temperature is very high.",
                            "evidence": {
                                "temperature_c": gpu_temperature,
                                "gpu": gpu.get("name"),
                            },
                        }
                    )
                elif isinstance(gpu_temperature, (int, float)) and gpu_temperature >= 80:
                    findings.append(
                        {
                            "severity": "attention",
                            "component": "gpu",
                            "message": "GPU temperature is elevated.",
                            "evidence": {
                                "temperature_c": gpu_temperature,
                                "gpu": gpu.get("name"),
                            },
                        }
                    )
                if (
                    isinstance(total, (int, float))
                    and total > 0
                    and isinstance(gpu_used, (int, float))
                    and gpu_used / total >= 0.95
                ):
                    findings.append(
                        {
                            "severity": "attention",
                            "component": "gpu_memory",
                            "message": "GPU VRAM is nearly full.",
                            "evidence": {"used_mib": gpu_used, "total_mib": total},
                        }
                    )

        network = telemetry.get("network")
        if isinstance(network, dict):
            adapters = CtypesWindowsBackend._json_list(network.get("adapters"))
            active = [item for item in adapters if item.get("up") and item.get("addresses")]
            if adapters and not active:
                findings.append(
                    {
                        "severity": "warning",
                        "component": "network",
                        "message": "No active network adapter with an IP address was detected.",
                        "evidence": {},
                    }
                )
            probe = network.get("connectivity_probe")
            if (
                isinstance(probe, dict)
                and not bool(probe.get("outbound_tcp"))
                and not bool(probe.get("dns_resolution"))
            ):
                findings.append(
                    {
                        "severity": "attention",
                        "component": "network",
                        "message": "The connectivity probes did not succeed.",
                        "evidence": probe,
                    }
                )

        windows = telemetry.get("windows")
        if isinstance(windows, dict):
            device_errors = CtypesWindowsBackend._json_list(windows.get("device_errors"))
            events = CtypesWindowsBackend._json_list(windows.get("serious_system_events"))
            if device_errors:
                findings.append(
                    {
                        "severity": "warning",
                        "component": "devices",
                        "message": (
                            f"Windows reports {len(device_errors)} device(s) with an error code."
                        ),
                        "evidence": {"count": len(device_errors)},
                    }
                )
            if events:
                findings.append(
                    {
                        "severity": "attention",
                        "component": "event_log",
                        "message": (
                            f"Windows logged {len(events)} critical/error system event(s) "
                            "in the last 24 hours."
                        ),
                        "evidence": {"count": len(events)},
                    }
                )
            if bool(windows.get("reboot_pending")):
                findings.append(
                    {
                        "severity": "info",
                        "component": "windows_update",
                        "message": "Windows reports a pending reboot.",
                        "evidence": {},
                    }
                )

        security = telemetry.get("security")
        if isinstance(security, dict):
            defender = security.get("defender")
            if isinstance(defender, dict) and defender.get("RealTimeProtectionEnabled") is False:
                findings.append(
                    {
                        "severity": "attention",
                        "component": "defender",
                        "message": (
                            "Microsoft Defender real-time protection is reported disabled."
                        ),
                        "evidence": {},
                    }
                )
            firewall = CtypesWindowsBackend._json_list(security.get("firewall"))
            disabled = [item.get("Name") for item in firewall if item.get("Enabled") is False]
            if disabled:
                findings.append(
                    {
                        "severity": "attention",
                        "component": "firewall",
                        "message": "One or more Windows Firewall profiles are disabled.",
                        "evidence": {"profiles": disabled},
                    }
                )

    @staticmethod
    def _diagnostic_summary(telemetry: dict[str, Any], findings: list[dict[str, Any]]) -> list[str]:
        summary: list[str] = []
        performance = telemetry.get("performance")
        if isinstance(performance, dict):
            memory = performance.get("memory")
            memory_percent = memory.get("used_percent") if isinstance(memory, dict) else None
            summary.append(f"CPU {performance.get('cpu_percent')}%; memory {memory_percent}% used.")
        hardware = telemetry.get("hardware")
        if isinstance(hardware, dict):
            gpus = CtypesWindowsBackend._json_list(hardware.get("gpus"))
            if gpus:
                gpu = gpus[0]
                details = [f"GPU {gpu.get('name') or 'adapter'}"]
                if gpu.get("utilization_percent") is not None:
                    details.append(f"{gpu.get('utilization_percent')}% load")
                if gpu.get("temperature_c") is not None:
                    details.append(f"{gpu.get('temperature_c')} C")
                if (
                    gpu.get("memory_used_mib") is not None
                    and gpu.get("memory_total_mib") is not None
                ):
                    details.append(
                        f"VRAM {gpu.get('memory_used_mib')}/{gpu.get('memory_total_mib')} MiB"
                    )
                elif gpu.get("memory_used_mib") is not None:
                    details.append(f"VRAM {gpu.get('memory_used_mib')} MiB in use")
                summary.append(", ".join(details) + ".")
        warning_count = sum(1 for item in findings if item.get("severity") == "warning")
        attention_count = sum(1 for item in findings if item.get("severity") == "attention")
        if warning_count or attention_count:
            summary.append(
                f"Diagnostics found {warning_count} warning(s) and "
                f"{attention_count} item(s) needing attention."
            )
        else:
            summary.append("No threshold-based problems were detected in the collected telemetry.")
        return summary[:6]

    @staticmethod
    def _spawn_silently(command: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            command,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def search_applications(self, query: str) -> list[dict[str, str]]:
        return [
            {"name": item.name, "source": item.source} for item in self._applications.search(query)
        ]

    def list_installed_applications(self) -> list[dict[str, str]]:
        return [{"name": item.name, "source": item.source} for item in self._applications.all()]

    def list_installed_games(self) -> list[dict[str, str]]:
        return [{"name": item.name, "source": item.source} for item in self._applications.games()]

    def refresh_application_index(self) -> int:
        return len(self._applications.refresh())

    @classmethod
    def _application_command(cls, requested: str) -> tuple[list[str], str]:
        normalized = requested.casefold()
        uri = cls._URI_APPLICATIONS.get(normalized)
        if uri is not None:
            return ["explorer.exe", uri], requested
        packaged_id = cls._PACKAGED_APPLICATIONS.get(normalized)
        if packaged_id is not None:
            return ["explorer.exe", f"shell:AppsFolder\\{packaged_id}"], requested

        executable = cls._APPLICATION_ALIASES.get(normalized, requested)
        on_path = shutil.which(executable)
        if on_path is not None:
            return [on_path], executable

        relative_paths = cls._DESKTOP_APPLICATION_PATHS.get(normalized, ())
        install_roots = (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        )
        for root in install_roots:
            if not root:
                continue
            for relative_path in relative_paths:
                candidate = Path(root, *relative_path.split("/"))
                if candidate.is_file():
                    arguments = (
                        ["--new-window", "about:blank"]
                        if normalized in {"chrome", "google chrome"}
                        else []
                    )
                    return [str(candidate), *arguments], candidate.name
        return [executable], executable

    def open_file(self, path: Path) -> None:
        if not path.exists():
            raise ToolExecutionError(
                "FILE_NOT_FOUND", "The requested file does not exist.", details={"path": str(path)}
            )
        try:
            os.startfile(str(path))
        except OSError as error:
            raise ToolExecutionError(
                "OPEN_FILE_FAILED", "Windows could not open the file."
            ) from error

    def control_media(self, action: str) -> None:
        key = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}[action]
        self._user32.keybd_event(key, 0, 0, 0)
        self._user32.keybd_event(key, 0, 0x0002, 0)

    def current_media(self) -> dict[str, str | bool | None]:
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )
        except ImportError as error:
            raise ToolExecutionError(
                "MEDIA_SESSION_SUPPORT_UNAVAILABLE",
                "Windows media-session support is not installed.",
            ) from error

        async def inspect() -> dict[str, str | bool | None]:
            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
            session = manager.get_current_session()
            if session is None:
                return {
                    "available": False,
                    "title": None,
                    "artist": None,
                    "album": None,
                    "source": None,
                    "status": None,
                }
            properties = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            status = {
                0: "closed",
                1: "opened",
                2: "changing",
                3: "stopped",
                4: "playing",
                5: "paused",
            }.get(int(playback.playback_status), "unknown")
            return {
                "available": True,
                "title": properties.title or None if properties is not None else None,
                "artist": properties.artist or None if properties is not None else None,
                "album": properties.album_title or None if properties is not None else None,
                "source": session.source_app_user_model_id or None,
                "status": status,
            }

        try:
            return asyncio.run(inspect())
        except Exception as error:
            raise ToolExecutionError(
                "MEDIA_SESSION_QUERY_FAILED",
                "Windows could not read the active media session.",
                details={"exception_type": error.__class__.__name__},
            ) from error

    def control_volume(self, action: str) -> None:
        key = {"up": 0xAF, "down": 0xAE, "mute_toggle": 0xAD}[action]
        self._user32.keybd_event(key, 0, 0, 0)
        self._user32.keybd_event(key, 0, 0x0002, 0)

    def control_master_audio(
        self, operation: str, amount: int | None = None, level: int | None = None
    ) -> dict[str, Any]:
        return self._audio.control_master(operation, amount, level)

    def list_audio_sessions(self) -> dict[str, Any]:
        return self._audio.list_sessions()

    def control_application_audio(
        self,
        application: str,
        operation: str,
        amount: int | None = None,
        level: int | None = None,
        scope: str = "all",
    ) -> dict[str, Any]:
        return self._audio.control_application(application, operation, amount, level, scope)

    def mute_audio_sessions_except(self, applications: list[str]) -> dict[str, Any]:
        return self._audio.mute_all_except(applications)

    def audio_diagnostic(self) -> dict[str, Any]:
        return self._audio.diagnostic()

    def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        callback_errors: list[Exception] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(handle: int, _: int) -> bool:
            try:
                if self._user32.IsWindowVisible(handle) and self._window_title(handle):
                    window = self._window_info(handle)
                    if window is not None:
                        windows.append(window)
                return True
            except Exception as error:
                callback_errors.append(error)
                return False

        callback_reference = callback_type(callback)
        ctypes.set_last_error(0)
        succeeded = bool(self._user32.EnumWindows(callback_reference, 0))
        windows_error = ctypes.get_last_error()
        if callback_errors or (not succeeded and windows_error):
            details = (
                {"exception_type": callback_errors[0].__class__.__name__}
                if callback_errors
                else {"winerror": windows_error}
            )
            raise ToolExecutionError(
                "WINDOW_ENUMERATION_FAILED", "Windows could not list windows.", details=details
            )
        return windows

    def find_windows(self, query: str) -> list[WindowInfo]:
        """Find visible windows by title or process, including titleless game windows."""
        compact = "".join(character for character in query.casefold() if character.isalnum())
        aliases = {"fileexplorer": "explorer"}
        compact = aliases.get(compact, compact)
        matches: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(handle: int, _: int) -> bool:
            if not self._user32.IsWindowVisible(handle):
                return True
            window = self._window_info(handle)
            if window is None:
                return True
            observed = "".join(
                character
                for character in f"{window.title} {window.application or ''}".casefold()
                if character.isalnum()
            )
            if compact and compact in observed:
                matches.append(window)
            return True

        callback_reference = callback_type(callback)
        if not self._user32.EnumWindows(callback_reference, 0):
            error = ctypes.get_last_error()
            if error:
                raise ToolExecutionError(
                    "WINDOW_ENUMERATION_FAILED", "Windows could not search open windows."
                )
        return matches

    def foreground_window(self) -> WindowInfo | None:
        handle = int(self._user32.GetForegroundWindow() or 0)
        return self._window_info(handle) if handle else None

    def focus_window(self, handle: int) -> bool:
        self._require_window(handle)
        show_command = SW_RESTORE if self._user32.IsIconic(handle) else SW_SHOW
        self._user32.ShowWindow(handle, show_command)

        def is_foreground() -> bool:
            return int(self._user32.GetForegroundWindow() or 0) == handle

        self._user32.BringWindowToTop(handle)
        self._user32.SetForegroundWindow(handle)
        time.sleep(0.1)
        if is_foreground():
            return True

        current_thread = int(self._kernel32.GetCurrentThreadId())
        foreground_handle = int(self._user32.GetForegroundWindow() or 0)
        foreground_thread = (
            int(self._user32.GetWindowThreadProcessId(foreground_handle, None))
            if foreground_handle
            else 0
        )
        target_thread = int(self._user32.GetWindowThreadProcessId(handle, None))
        attached_threads: list[int] = []
        try:
            for thread_id in {foreground_thread, target_thread}:
                if (
                    thread_id
                    and thread_id != current_thread
                    and self._user32.AttachThreadInput(current_thread, thread_id, True)
                ):
                    attached_threads.append(thread_id)
            self._user32.BringWindowToTop(handle)
            self._user32.SetActiveWindow(handle)
            self._user32.SetFocus(handle)
            self._user32.SetForegroundWindow(handle)
        finally:
            for thread_id in reversed(attached_threads):
                self._user32.AttachThreadInput(current_thread, thread_id, False)
        time.sleep(0.1)
        if is_foreground():
            return True

        position_flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        self._user32.SetWindowPos(handle, -1, 0, 0, 0, 0, position_flags)
        self._user32.SetWindowPos(handle, -2, 0, 0, 0, 0, position_flags)
        self._user32.SetForegroundWindow(handle)
        time.sleep(0.1)
        if is_foreground():
            return True
        raise ToolExecutionError(
            "FOREGROUND_ACCESS_DENIED",
            "Windows did not allow that window to become foreground.",
            retryable=True,
            details={"window_handle": handle},
        )

    def minimize_window(self, handle: int) -> bool:
        self._require_window(handle)
        self._user32.ShowWindow(handle, SW_MINIMIZE)
        return bool(self._user32.IsIconic(handle))

    def maximize_window(self, handle: int) -> bool:
        self._require_window(handle)
        self._user32.ShowWindow(handle, SW_MAXIMIZE)
        return bool(self._user32.IsZoomed(handle))

    def restore_window(self, handle: int) -> bool:
        self._require_window(handle)
        self._user32.ShowWindow(handle, SW_RESTORE)
        return not bool(self._user32.IsIconic(handle)) and not bool(self._user32.IsZoomed(handle))

    def close_window(self, handle: int, timeout_seconds: float = 3) -> bool:
        self._require_window(handle)
        if not self._user32.PostMessageW(handle, WM_CLOSE, 0, 0):
            raise ToolExecutionError("WINDOW_CLOSE_FAILED", "Windows rejected the close request.")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self._user32.IsWindow(handle):
                return True
            time.sleep(0.05)
        return not bool(self._user32.IsWindow(handle))

    def move_window_to_monitor(
        self, handle: int, destination: MonitorDestination
    ) -> WindowMoveOutcome:
        self._require_window(handle)
        monitors = self.list_monitors()
        current = self._window_info(handle)
        if current is None or current.rectangle is None or current.monitor_id is None:
            raise ToolExecutionError("WINDOW_BOUNDS_UNAVAILABLE", "Window bounds are unavailable.")
        source = next(
            (item for item in monitors if item.monitor_id == current.monitor_id),
            None,
        )
        if source is None:
            raise ToolExecutionError(
                "SOURCE_MONITOR_UNAVAILABLE",
                "Windows could not identify the window's current monitor.",
            )
        if destination.relation == "previous":
            previous_id = self._previous_monitor_by_window.get(handle)
            target = next(
                (item for item in monitors if item.monitor_id == previous_id),
                None,
            )
            if target is None:
                raise ToolExecutionError(
                    "PREVIOUS_MONITOR_UNAVAILABLE",
                    "No previous monitor is recorded for that window yet.",
                )
        else:
            target = _resolve_monitor_destination(monitors, source, destination)
        was_minimized = bool(self._user32.IsIconic(handle))
        was_maximized = bool(self._user32.IsZoomed(handle))
        preserved_state = (
            "minimized" if was_minimized else "maximized" if was_maximized else "normal"
        )
        changed = source.monitor_id != target.monitor_id
        if not changed:
            return WindowMoveOutcome(
                verified=True,
                changed=False,
                destination=destination,
                source_monitor=source,
                target_monitor=target,
                observed_monitor=source,
                preserved_state=preserved_state,
            )

        # Restoring first gives Windows a real normal rectangle to transfer. The original
        # minimized/maximized state is reapplied immediately after the move.
        if was_minimized or was_maximized:
            self._user32.ShowWindow(handle, SW_RESTORE)
            time.sleep(0.05)
            restored = self._window_info(handle)
            if restored is not None and restored.rectangle is not None:
                current = restored

        assert current.rectangle is not None
        left, top, width, height = _translated_window_rectangle(
            current.rectangle, source.work_area, target.work_area
        )
        if not self._user32.MoveWindow(handle, left, top, width, height, True):
            raise ToolExecutionError("WINDOW_MOVE_FAILED", "Windows rejected the move request.")

        if was_maximized:
            self._user32.ShowWindow(handle, SW_MAXIMIZE)
        elif was_minimized:
            self._user32.ShowWindow(handle, SW_MINIMIZE)

        updated = self._window_info(handle)
        observed = (
            next(
                (item for item in monitors if updated and item.monitor_id == updated.monitor_id),
                None,
            )
            if updated is not None
            else None
        )
        verified = observed is not None and observed.monitor_id == target.monitor_id
        if verified:
            self._previous_monitor_by_window[handle] = source.monitor_id
        return WindowMoveOutcome(
            verified=verified,
            changed=verified,
            destination=destination,
            source_monitor=source,
            target_monitor=target,
            observed_monitor=observed,
            preserved_state=preserved_state,
        )

    def list_monitors(self) -> list[MonitorInfo]:
        monitors: list[MonitorInfo] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def callback(handle: int, _dc: int, _rect: Any, _data: int) -> bool:
            info = self._monitor_info(handle)
            if info is not None:
                monitors.append(info)
            return True

        callback_reference = callback_type(callback)
        if not self._user32.EnumDisplayMonitors(0, None, callback_reference, 0):
            raise ToolExecutionError(
                "MONITOR_ENUMERATION_FAILED", "Windows could not list monitors."
            )
        return _decorate_monitor_topology(monitors)

    def _window_info(self, handle: int) -> WindowInfo | None:
        if not handle or not self._user32.IsWindow(handle):
            return None
        process_id = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        rectangle = wintypes.RECT()
        bounds = None
        if self._user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            bounds = _rect(rectangle)
        monitor_handle = self._user32.MonitorFromWindow(handle, MONITOR_DEFAULTTONEAREST)
        monitor = self._monitor_info(int(monitor_handle or 0))
        try:
            application = psutil.Process(process_id.value).name()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            application = None
        return WindowInfo(
            handle=handle,
            title=self._window_title(handle),
            process_id=process_id.value,
            application=application,
            rectangle=bounds,
            monitor_id=monitor.monitor_id if monitor else None,
            minimized=bool(self._user32.IsIconic(handle)),
            maximized=bool(self._user32.IsZoomed(handle)),
        )

    def _window_title(self, handle: int) -> str:
        length = int(self._user32.GetWindowTextLengthW(handle))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def _monitor_info(self, handle: int) -> MonitorInfo | None:
        if not handle:
            return None
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return None
        device_name = str(info.szDevice)
        return MonitorInfo(
            monitor_id=device_name,
            device_name=device_name,
            rectangle=_rect(info.rcMonitor),
            work_area=_rect(info.rcWork),
            primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
            number=_display_number(device_name),
            friendly_name=self._display_friendly_name(device_name),
        )

    def _display_friendly_name(self, device_name: str) -> str | None:
        display = DISPLAY_DEVICEW()
        display.cb = ctypes.sizeof(display)
        if not self._user32.EnumDisplayDevicesW(device_name, 0, ctypes.byref(display), 0):
            return None
        value = str(display.DeviceString).strip()
        return value or None

    def _require_window(self, handle: int) -> None:
        if not self._user32.IsWindow(handle):
            raise ToolExecutionError(
                "WINDOW_NOT_FOUND",
                "The requested window no longer exists.",
                details={"handle": handle},
            )


def _rect(value: wintypes.RECT) -> Rect:
    return Rect(left=value.left, top=value.top, right=value.right, bottom=value.bottom)


def _optional_string(value: object) -> str | None:
    return str(value) if value else None


def _audio_int_option(options: dict[str, object], key: str, default: int) -> int:
    value = options.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _audio_float_option(options: dict[str, object], key: str, default: float) -> float:
    value = options.get(key)
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _audio_bool_option(options: dict[str, object], key: str, default: bool) -> bool:
    value = options.get(key)
    return value if isinstance(value, bool) else default


def _select_monitor(monitors: list[MonitorInfo], requested: str) -> MonitorInfo:
    normalized = " ".join(requested.strip().casefold().split())
    if normalized in {"primary", "primary monitor", "main", "main monitor"}:
        return next((item for item in monitors if item.primary), monitors[0])
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
    if number is not None:
        target = next((item for item in monitors if item.number == number), None)
        if target is not None:
            return target
    for monitor in monitors:
        identities = {
            monitor.device_name.casefold(),
            (monitor.label or "").casefold(),
            (monitor.friendly_name or "").casefold(),
        }
        if normalized in identities:
            return monitor
    raise ToolExecutionError(
        "MONITOR_NOT_FOUND",
        "The requested monitor could not be found.",
        details={
            "monitor": requested,
            "available": [item.label or item.device_name for item in monitors],
        },
    )


def _display_number(device_name: str) -> int | None:
    match = re.search(r"DISPLAY(\d+)$", device_name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _monitor_label(monitor: MonitorInfo, fallback_number: int) -> str:
    number = monitor.number or fallback_number
    return f"monitor {number}"


def _decorate_monitor_topology(monitors: list[MonitorInfo]) -> list[MonitorInfo]:
    if not monitors:
        return []
    ordered = sorted(
        monitors,
        key=lambda item: (
            item.number is None,
            item.number or 0,
            item.rectangle.left,
            item.rectangle.top,
        ),
    )
    primary = next((item for item in ordered if item.primary), ordered[0])
    decorated: list[MonitorInfo] = []
    for index, monitor in enumerate(ordered, start=1):
        decorated.append(
            monitor.model_copy(
                update={
                    "number": monitor.number or index,
                    "label": _monitor_label(monitor, index),
                    "relative_position": _relative_position(primary, monitor),
                }
            )
        )
    return decorated


def _relative_position(primary: MonitorInfo, monitor: MonitorInfo) -> str:
    if monitor.monitor_id == primary.monitor_id:
        return "primary"
    px, py = _rect_center(primary.rectangle)
    mx, my = _rect_center(monitor.rectangle)
    horizontal = "left" if mx < px else "right" if mx > px else ""
    vertical = "above" if my < py else "below" if my > py else ""
    if horizontal and vertical:
        return f"{vertical}-{horizontal}"
    return horizontal or vertical or "overlapping"


def _rect_center(rectangle: Rect) -> tuple[float, float]:
    return (
        (rectangle.left + rectangle.right) / 2.0,
        (rectangle.top + rectangle.bottom) / 2.0,
    )


def _resolve_monitor_destination(
    monitors: list[MonitorInfo], source: MonitorInfo, destination: MonitorDestination
) -> MonitorInfo:
    if destination.number is not None:
        target = next((item for item in monitors if item.number == destination.number), None)
        if target is not None:
            return target
        raise ToolExecutionError(
            "MONITOR_NOT_FOUND",
            f"Monitor {destination.number} is not active.",
            details={"number": destination.number, "available": [item.number for item in monitors]},
        )
    if destination.device_name is not None:
        normalized = destination.device_name.strip().casefold()
        target = next(
            (
                item
                for item in monitors
                if normalized
                in {
                    item.device_name.casefold(),
                    (item.label or "").casefold(),
                    (item.friendly_name or "").casefold(),
                }
            ),
            None,
        )
        if target is not None:
            return target
        raise ToolExecutionError(
            "MONITOR_NOT_FOUND",
            "The requested Windows display is not active.",
            details={"device_name": destination.device_name},
        )

    relation = destination.relation
    assert relation is not None
    if relation == "previous":
        raise ToolExecutionError(
            "PREVIOUS_MONITOR_REQUIRES_WINDOW",
            "Previous-monitor selection requires a window-specific move.",
        )
    if relation == "primary":
        return next((item for item in monitors if item.primary), monitors[0])
    others = [item for item in monitors if item.monitor_id != source.monitor_id]
    if not others:
        raise ToolExecutionError(
            "OTHER_MONITOR_UNAVAILABLE", "No other monitor is currently available."
        )
    if relation == "other":
        if len(others) == 1:
            return others[0]
        raise ToolExecutionError(
            "AMBIGUOUS_MONITOR",
            "More than one other monitor is available. Specify left, right, above, below, "
            "or a monitor number.",
            details={"candidates": [item.label or item.device_name for item in others]},
        )
    if relation == "nearest":
        sx, sy = _rect_center(source.rectangle)
        return min(
            others,
            key=lambda item: math.hypot(
                _rect_center(item.rectangle)[0] - sx,
                _rect_center(item.rectangle)[1] - sy,
            ),
        )
    return _select_spatial_monitor(source, others, relation)


def _select_spatial_monitor(
    source: MonitorInfo, candidates: list[MonitorInfo], relation: str
) -> MonitorInfo:
    sx, sy = _rect_center(source.rectangle)
    directional: list[tuple[tuple[float, float, float], MonitorInfo]] = []
    for candidate in candidates:
        cx, cy = _rect_center(candidate.rectangle)
        if relation == "right" and cx <= sx:
            continue
        if relation == "left" and cx >= sx:
            continue
        if relation == "below" and cy <= sy:
            continue
        if relation == "above" and cy >= sy:
            continue
        if relation in {"left", "right"}:
            perpendicular_gap = _interval_gap(
                source.rectangle.top,
                source.rectangle.bottom,
                candidate.rectangle.top,
                candidate.rectangle.bottom,
            )
            forward_gap = abs(cx - sx)
        else:
            perpendicular_gap = _interval_gap(
                source.rectangle.left,
                source.rectangle.right,
                candidate.rectangle.left,
                candidate.rectangle.right,
            )
            forward_gap = abs(cy - sy)
        distance = math.hypot(cx - sx, cy - sy)
        directional.append(((perpendicular_gap, forward_gap, distance), candidate))
    if directional:
        return min(directional, key=lambda item: item[0])[1]
    raise ToolExecutionError(
        "MONITOR_DIRECTION_UNAVAILABLE",
        f"There is no active monitor {relation} of {source.label or source.device_name}.",
        details={"relation": relation, "source": source.label or source.device_name},
    )


def _interval_gap(start_a: int, end_a: int, start_b: int, end_b: int) -> float:
    if end_a < start_b:
        return float(start_b - end_a)
    if end_b < start_a:
        return float(start_a - end_b)
    return 0.0


def _translated_window_rectangle(
    window: Rect, source: Rect, target: Rect
) -> tuple[int, int, int, int]:
    source_width = max(1, source.right - source.left)
    source_height = max(1, source.bottom - source.top)
    target_width = max(1, target.right - target.left)
    target_height = max(1, target.bottom - target.top)
    width = min(max(120, window.right - window.left), target_width)
    height = min(max(80, window.bottom - window.top), target_height)
    source_x_range = max(1, source_width - width)
    source_y_range = max(1, source_height - height)
    relative_x = min(1.0, max(0.0, (window.left - source.left) / source_x_range))
    relative_y = min(1.0, max(0.0, (window.top - source.top) / source_y_range))
    target_x_range = max(0, target_width - width)
    target_y_range = max(0, target_height - height)
    left = target.left + round(relative_x * target_x_range)
    top = target.top + round(relative_y * target_y_range)
    return left, top, width, height
