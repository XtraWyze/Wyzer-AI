"""Built-in model-safe Windows desktop UI interaction tools.

The model never receives screen coordinates or raw window handles. Inspection
returns opaque, self-contained element references that later calls re-resolve
inside an isolated worker.
"""

from __future__ import annotations

import base64
import json
import platform
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments
from wyzer.tools.base import Tool, ToolContext, ToolExecutionError


class InspectDesktopArguments(ToolArguments):
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="Optional visible label or control type to filter for, such as Save, search box, button, or edit.",
    )
    limit: int = Field(
        default=40, ge=1, le=100, description="Maximum number of useful controls to return."
    )


class ClickDesktopElementArguments(ToolArguments):
    element_id: str = Field(
        min_length=8,
        max_length=4096,
        description="Opaque element_id returned by inspect_desktop_ui. Never invent this value.",
    )


class TypeDesktopTextArguments(ToolArguments):
    text: str = Field(
        min_length=1,
        max_length=10000,
        description="Literal text to type into the currently focused desktop control.",
    )
    target_window: str = Field(
        min_length=1,
        max_length=260,
        description="Expected focused application name or visible window-title text.",
    )


class PressDesktopKeyArguments(ToolArguments):
    key: Literal[
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "page_up",
        "page_down",
        "space",
        "ctrl_a",
        "ctrl_c",
        "ctrl_v",
        "ctrl_x",
        "ctrl_z",
        "ctrl_y",
        "alt_f4",
    ] = Field(description="Keyboard key or shortcut to send to the focused desktop application.")
    presses: int = Field(default=1, ge=1, le=20, description="How many times to press the key.")
    target_window: str = Field(
        min_length=1,
        max_length=260,
        description="Expected focused application name or visible window-title text.",
    )


class DesktopElement(BaseModel):
    element_id: str
    name: str
    control_type: str
    automation_id: str | None = None
    enabled: bool = True
    visible: bool = True


class DesktopInspectionResult(BaseModel):
    window_title: str
    application: str | None = None
    elements: list[DesktopElement]
    truncated: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class DesktopActionResult(BaseModel):
    action: str
    target: str
    success: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class DesktopUIAdapter(Protocol):
    available: bool
    unavailable_reason: str | None

    def inspect(self, query: str | None, limit: int) -> DesktopInspectionResult: ...
    def click(self, element_id: str) -> DesktopActionResult: ...
    def type_text(self, text: str) -> DesktopActionResult: ...
    def press_key(self, key: str, presses: int) -> DesktopActionResult: ...


def _encode_reference(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "dui_" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_reference(value: str) -> dict[str, Any]:
    if not value.startswith("dui_"):
        raise ToolExecutionError(
            "INVALID_ELEMENT_REFERENCE",
            "The desktop element reference is invalid. Inspect the desktop again.",
        )
    encoded = value[4:]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        data = json.loads(raw.decode("utf-8"))
    except Exception as error:
        raise ToolExecutionError(
            "INVALID_ELEMENT_REFERENCE",
            "The desktop element reference is invalid. Inspect the desktop again.",
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("hwnd"), int):
        raise ToolExecutionError(
            "INVALID_ELEMENT_REFERENCE",
            "The desktop element reference is invalid. Inspect the desktop again.",
        )
    return data


class PywinautoDesktopAdapter:
    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason: str | None = None
        self._Desktop: Any = None
        self._keyboard: Any = None
        if platform.system() != "Windows":
            self.unavailable_reason = "desktop UI interaction is available only on Windows"
            return
        try:
            from pywinauto import Desktop, keyboard
        except Exception as error:
            self.unavailable_reason = f"pywinauto is unavailable: {error}"
            return
        self._Desktop = Desktop
        self._keyboard = keyboard
        self.available = True

    def _foreground(self) -> Any:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            raise ToolExecutionError(
                "NO_FOREGROUND_WINDOW", "I couldn't find a focused desktop window."
            )
        try:
            return self._Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        except Exception as error:
            raise ToolExecutionError(
                "UI_INSPECTION_FAILED",
                "I couldn't inspect the focused desktop window.",
                retryable=True,
                details={"exception_type": error.__class__.__name__},
            ) from error

    def inspect(self, query: str | None, limit: int) -> DesktopInspectionResult:
        root = self._foreground()
        hwnd = int(root.handle)
        title = str(root.window_text() or "")
        process_name: str | None = None
        try:
            import psutil

            process_name = psutil.Process(int(root.process_id())).name()
        except Exception:
            pass
        needle = (query or "").casefold().strip()
        useful_types = {
            "Button",
            "Edit",
            "ComboBox",
            "ListItem",
            "MenuItem",
            "TabItem",
            "CheckBox",
            "RadioButton",
            "Hyperlink",
            "TreeItem",
            "DataItem",
            "Text",
        }
        rows: list[DesktopElement] = []
        counts: dict[tuple[str, str, str], int] = {}
        try:
            descendants = root.descendants()
        except Exception as error:
            raise ToolExecutionError(
                "UI_INSPECTION_FAILED",
                "I couldn't enumerate controls in the focused window.",
                retryable=True,
            ) from error
        for control in descendants:
            try:
                info = control.element_info
                name = str(getattr(info, "name", "") or "").strip()
                control_type = str(getattr(info, "control_type", "") or "").strip()
                automation_id = str(getattr(info, "automation_id", "") or "").strip()
                if control_type not in useful_types:
                    continue
                searchable = f"{name} {control_type} {automation_id}".casefold()
                if needle and needle not in searchable:
                    continue
                if not name and not automation_id and control_type == "Text":
                    continue
                key = (name, control_type, automation_id)
                ordinal = counts.get(key, 0)
                counts[key] = ordinal + 1
                ref = _encode_reference(
                    {
                        "v": 1,
                        "hwnd": hwnd,
                        "name": name,
                        "type": control_type,
                        "automation_id": automation_id,
                        "ordinal": ordinal,
                    }
                )
                try:
                    enabled = bool(control.is_enabled())
                    visible = bool(control.is_visible())
                except Exception:
                    enabled = visible = True
                rows.append(
                    DesktopElement(
                        element_id=ref,
                        name=name or automation_id or control_type,
                        control_type=control_type,
                        automation_id=automation_id or None,
                        enabled=enabled,
                        visible=visible,
                    )
                )
                if len(rows) >= limit:
                    break
            except Exception:
                continue
        return DesktopInspectionResult(
            window_title=title,
            application=process_name,
            elements=rows,
            truncated=len(rows) >= limit,
            evidence={
                "verification_status": "verified",
                "predicate": "foreground_window_inspected",
                "observed": {"title": title, "element_count": len(rows)},
            },
        )

    def _resolve(self, reference: dict[str, Any]) -> Any:
        hwnd = reference["hwnd"]
        try:
            root = self._Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        except Exception as error:
            raise ToolExecutionError(
                "STALE_ELEMENT_REFERENCE",
                "That window is no longer available. Inspect the desktop again.",
                retryable=True,
            ) from error
        matches: list[Any] = []
        for control in root.descendants():
            try:
                info = control.element_info
                if str(getattr(info, "control_type", "") or "") != reference.get("type", ""):
                    continue
                automation_id = str(getattr(info, "automation_id", "") or "")
                name = str(getattr(info, "name", "") or "")
                expected_id = reference.get("automation_id", "")
                expected_name = reference.get("name", "")
                if expected_id and automation_id != expected_id:
                    continue
                if not expected_id and name != expected_name:
                    continue
                matches.append(control)
            except Exception:
                continue
        ordinal = int(reference.get("ordinal", 0))
        if ordinal < 0 or ordinal >= len(matches):
            raise ToolExecutionError(
                "STALE_ELEMENT_REFERENCE",
                "That control changed or disappeared. Inspect the desktop again.",
                retryable=True,
            )
        return matches[ordinal]

    def click(self, element_id: str) -> DesktopActionResult:
        reference = _decode_reference(element_id)
        control = self._resolve(reference)
        label = str(
            reference.get("name")
            or reference.get("automation_id")
            or reference.get("type")
            or "control"
        )
        try:
            control.set_focus()
            try:
                control.invoke()
            except Exception:
                control.click_input()
        except Exception as error:
            raise ToolExecutionError(
                "UI_CLICK_FAILED", f"I couldn't activate {label}.", retryable=True
            ) from error
        return DesktopActionResult(
            action="click",
            target=label,
            evidence={
                "verification_status": "not_verified",
                "predicate": "element_activation_requested",
                "observed": {"target": label},
            },
        )

    def type_text(self, text: str) -> DesktopActionResult:
        try:
            self._keyboard.send_keys(
                text,
                with_spaces=True,
                with_tabs=True,
                with_newlines=True,
                pause=0.01,
                vk_packet=True,
            )
        except Exception as error:
            raise ToolExecutionError(
                "DESKTOP_TYPING_FAILED",
                "I couldn't type into the focused desktop control.",
                retryable=True,
            ) from error
        return DesktopActionResult(
            action="type_text",
            target="focused control",
            evidence={
                "verification_status": "not_verified",
                "predicate": "text_input_sent",
                "observed": {"character_count": len(text)},
            },
        )

    def press_key(self, key: str, presses: int) -> DesktopActionResult:
        mapping = {
            "enter": "{ENTER}",
            "tab": "{TAB}",
            "escape": "{ESC}",
            "backspace": "{BACKSPACE}",
            "delete": "{DELETE}",
            "up": "{UP}",
            "down": "{DOWN}",
            "left": "{LEFT}",
            "right": "{RIGHT}",
            "home": "{HOME}",
            "end": "{END}",
            "page_up": "{PGUP}",
            "page_down": "{PGDN}",
            "space": "{SPACE}",
            "ctrl_a": "^a",
            "ctrl_c": "^c",
            "ctrl_v": "^v",
            "ctrl_x": "^x",
            "ctrl_z": "^z",
            "ctrl_y": "^y",
            "alt_f4": "%{F4}",
        }
        sequence = mapping[key]
        try:
            for _ in range(presses):
                self._keyboard.send_keys(sequence, pause=0.03)
        except Exception as error:
            raise ToolExecutionError(
                "KEY_PRESS_FAILED", f"I couldn't press {key}.", retryable=True
            ) from error
        return DesktopActionResult(
            action="press_key",
            target=key,
            evidence={
                "verification_status": "not_verified",
                "predicate": "key_input_sent",
                "observed": {"key": key, "presses": presses},
            },
        )


class _DesktopTool(Tool[Any, DesktopActionResult]):
    def __init__(self, adapter: DesktopUIAdapter) -> None:
        self.adapter = adapter
        self.available = adapter.available
        self.unavailable_reason = adapter.unavailable_reason

    def _require_target_window(self, expected: str) -> None:
        inspected = self.adapter.inspect(None, 1)
        observed = f"{inspected.window_title} {inspected.application or ''}"
        compact_expected = "".join(
            character for character in expected.casefold() if character.isalnum()
        )
        compact_observed = "".join(
            character for character in observed.casefold() if character.isalnum()
        )
        if compact_expected not in compact_observed:
            raise ToolExecutionError(
                "FOCUSED_WINDOW_CHANGED",
                f"The focused window is no longer {expected}. Inspect or focus it again before retrying.",
                retryable=True,
                details={
                    "expected_window": expected,
                    "observed_window": inspected.window_title,
                    "observed_application": inspected.application,
                },
            )


class InspectDesktopUITool(Tool[InspectDesktopArguments, DesktopInspectionResult]):
    name = "inspect_desktop_ui"
    llm_visible = False
    description = "Inspect useful controls in the currently focused Windows app and return element IDs for later clicking."
    arguments_type = InspectDesktopArguments
    result_type = DesktopInspectionResult
    risk_level = RiskLevel.LOW
    read_only = True
    confirmation = ConfirmationMode.NEVER

    def __init__(self, adapter: DesktopUIAdapter) -> None:
        self.adapter = adapter
        self.available = adapter.available
        self.unavailable_reason = adapter.unavailable_reason

    def execute(
        self, arguments: InspectDesktopArguments, context: ToolContext
    ) -> DesktopInspectionResult:
        return self.adapter.inspect(arguments.query, arguments.limit)


class ClickDesktopElementTool(_DesktopTool):
    name = "click_desktop_element"
    llm_visible = False
    description = (
        "Activate one Windows UI control using an element_id returned by inspect_desktop_ui."
    )
    arguments_type = ClickDesktopElementArguments
    result_type = DesktopActionResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.NEVER

    def execute(
        self, arguments: ClickDesktopElementArguments, context: ToolContext
    ) -> DesktopActionResult:
        return self.adapter.click(arguments.element_id)


class TypeDesktopTextTool(_DesktopTool):
    name = "type_desktop_text"
    description = (
        "Type literal text into the focused control only after verifying that the expected "
        "Windows application is still focused."
    )
    arguments_type = TypeDesktopTextArguments
    result_type = DesktopActionResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.NEVER

    def execute(
        self, arguments: TypeDesktopTextArguments, context: ToolContext
    ) -> DesktopActionResult:
        del context
        self._require_target_window(arguments.target_window)
        return self.adapter.type_text(arguments.text)


class PressDesktopKeyTool(_DesktopTool):
    name = "press_desktop_key"
    description = (
        "Press a common key or shortcut only after verifying that the expected Windows "
        "application is still focused."
    )
    arguments_type = PressDesktopKeyArguments
    result_type = DesktopActionResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.CONDITIONAL

    def execute(
        self, arguments: PressDesktopKeyArguments, context: ToolContext
    ) -> DesktopActionResult:
        del context
        self._require_target_window(arguments.target_window)
        return self.adapter.press_key(arguments.key, arguments.presses)


@dataclass(frozen=True, slots=True)
class DesktopInteractionPack:
    name: str = "desktop_interaction"
    adapter: DesktopUIAdapter | None = None

    def create_tools(self) -> tuple[Tool[Any, Any], ...]:
        adapter = self.adapter or PywinautoDesktopAdapter()
        return (
            InspectDesktopUITool(adapter),
            ClickDesktopElementTool(adapter),
            TypeDesktopTextTool(adapter),
            PressDesktopKeyTool(adapter),
        )


def create_desktop_interaction_pack() -> DesktopInteractionPack:
    """Create the built-in desktop interaction capability pack."""
    return DesktopInteractionPack()


# Backward-friendly module-level alias for pack-oriented tests/extensions.
create_pack = create_desktop_interaction_pack
