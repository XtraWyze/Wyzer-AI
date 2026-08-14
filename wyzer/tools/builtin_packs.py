"""Curated built-in capability packs for the Windows assistant.

Only user-facing, reliable tools belong here. Low-level window-handle tools,
legacy website launchers and duplicate low-level actions are intentionally excluded.
Browser, clipboard, and model-safe desktop UI interaction are first-class built-ins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wyzer.desktop.system import WindowsSystemBackend
from wyzer.tools.base import Tool
from wyzer.tools.browser import create_browser_pack
from wyzer.tools.capabilities import (
    ActivateToolCapabilityTool,
    ListToolCapabilitiesTool,
)
from wyzer.tools.clipboard import create_clipboard_pack
from wyzer.tools.desktop_interaction import create_desktop_interaction_pack
from wyzer.tools.diagnostics import DiagnoseSystemTool
from wyzer.tools.packs import SimpleToolPack, ToolPack
from wyzer.tools.perception import create_perception_pack
from wyzer.tools.windows import (
    ControlApplicationAudioTool,
    ControlMasterAudioTool,
    ControlMediaTool,
    ControlNamedWindowTool,
    GetCurrentMediaTool,
    GetForegroundWindowTool,
    GetMonitorLayoutTool,
    GetSystemProfileTool,
    IsProcessRunningTool,
    ListAudioSessionsTool,
    ListInstalledApplicationsTool,
    ListInstalledGamesTool,
    ListOpenWindowsTool,
    ListRunningProcessesTool,
    MoveNamedWindowToMonitorTool,
    MuteAllAudioExceptTool,
    OpenApplicationTool,
    OpenFileTool,
    RefreshApplicationIndexTool,
    SearchInstalledApplicationsTool,
    WaitMsTool,
)


@dataclass(frozen=True, slots=True)
class BackendToolPack:
    """Create one named pack from backend-aware tool classes."""

    name: str
    backend: WindowsSystemBackend
    tool_types: tuple[type[Any], ...]
    description: str = ""
    activation_name: str = ""

    def create_tools(self) -> tuple[Tool[Any, Any], ...]:
        return tuple(
            tool_type() if tool_type is WaitMsTool else tool_type(self.backend)
            for tool_type in self.tool_types
        )


def create_builtin_packs(
    backend: WindowsSystemBackend,
    perception_options: dict[str, object] | None = None,
) -> tuple[ToolPack, ...]:
    """Return the default packs in a stable registration order."""

    return (
        SimpleToolPack(
            "capabilities",
            (ListToolCapabilitiesTool, ActivateToolCapabilityTool),
            "Discover and activate optional native tool packs.",
        ),
        BackendToolPack(
            "applications",
            backend,
            (
                OpenApplicationTool,
                SearchInstalledApplicationsTool,
                ListInstalledApplicationsTool,
                RefreshApplicationIndexTool,
                ListInstalledGamesTool,
                OpenFileTool,
            ),
            "Launch and discover installed Windows applications and open files in their apps.",
        ),
        BackendToolPack(
            "audio",
            backend,
            (
                ControlMasterAudioTool,
                ListAudioSessionsTool,
                ControlApplicationAudioTool,
                MuteAllAudioExceptTool,
            ),
            "Read and control Windows master and per-application audio.",
        ),
        create_browser_pack(),
        create_clipboard_pack(),
        create_desktop_interaction_pack(),
        create_perception_pack(perception_options or {}),
        BackendToolPack(
            "media",
            backend,
            (
                ControlMediaTool,
                GetCurrentMediaTool,
            ),
            "Read and control the active Windows media session.",
        ),
        BackendToolPack(
            "diagnostics",
            backend,
            (DiagnoseSystemTool,),
            "Run bounded read-only Windows health diagnostics.",
            "diagnostics",
        ),
        BackendToolPack(
            "system",
            backend,
            (
                GetSystemProfileTool,
                ListRunningProcessesTool,
                IsProcessRunningTool,
                WaitMsTool,
            ),
            "Read Windows system, hardware, and background-process state.",
        ),
        BackendToolPack(
            "windows",
            backend,
            (
                GetForegroundWindowTool,
                ListOpenWindowsTool,
                ControlNamedWindowTool,
                MoveNamedWindowToMonitorTool,
                GetMonitorLayoutTool,
            ),
            "Observe and control desktop windows and monitor placement.",
        ),
    )


BUILTIN_PACK_NAMES = (
    "capabilities",
    "applications",
    "audio",
    "browser",
    "clipboard",
    "coding_agent",
    "desktop_interaction",
    "diagnostics",
    "perception",
    "files",
    "media",
    "system",
    "windows",
)


DEFAULT_CAPABILITY_PACKS = frozenset(
    {"applications", "audio", "capabilities", "media", "system", "windows"}
)
