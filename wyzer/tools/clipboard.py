"""Built-in Wyzer clipboard and selection tools for Windows."""

from __future__ import annotations

import ctypes
import os
import time
import uuid

import psutil
import pyperclip
from pydantic import BaseModel, Field

from wyzer.models import RiskLevel, ToolArguments
from wyzer.tools.base import ToolContext, ToolExecutionError
from wyzer.tools.packs import CallableTool, SimpleToolPack


class NoArguments(ToolArguments):
    pass


class WriteClipboardArguments(ToolArguments):
    text: str = Field(
        max_length=100_000,
        description="The exact text the user explicitly asked to place on the clipboard.",
    )


class FocusedWindowArguments(ToolArguments):
    target_window: str = Field(
        min_length=1,
        max_length=260,
        description="Expected focused application name or visible window-title text.",
    )


class ClipboardResult(BaseModel):
    text: str


class ClipboardWriteResult(BaseModel):
    characters_written: int


class CopySelectedTextResult(BaseModel):
    copied: bool
    text: str | None = None
    message: str


class PasteClipboardResult(BaseModel):
    pasted: bool
    message: str


_VK_CONTROL = 0x11
_VK_C = 0x43
_VK_V = 0x56
_KEYEVENTF_KEYUP = 0x0002


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Selection copy and paste tools are only supported on Windows.")


def _send_ctrl_shortcut(key: int) -> None:
    """Send a Ctrl+key shortcut to the currently focused Windows application."""

    _require_windows()
    user32 = ctypes.windll.user32
    user32.keybd_event(_VK_CONTROL, 0, 0, 0)
    try:
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, _KEYEVENTF_KEYUP, 0)
    finally:
        user32.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)


def _require_focused_window(expected: str) -> str:
    _require_windows()
    user32 = ctypes.windll.user32
    handle = int(user32.GetForegroundWindow() or 0)
    if not handle:
        raise ToolExecutionError(
            "NO_FOREGROUND_WINDOW",
            "No focused window is available for the clipboard action.",
        )
    length = int(user32.GetWindowTextLengthW(handle))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
    try:
        process_name = psutil.Process(int(process_id.value)).name()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        process_name = ""
    observed = f"{buffer.value} {process_name}"
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
            details={"expected_window": expected, "observed_window": buffer.value},
        )
    return buffer.value or process_name


def _read_clipboard(
    arguments: NoArguments,
    context: ToolContext,
) -> ClipboardResult:
    del arguments, context
    return ClipboardResult(text=pyperclip.paste())


def _write_clipboard(
    arguments: WriteClipboardArguments,
    context: ToolContext,
) -> ClipboardWriteResult:
    del context
    pyperclip.copy(arguments.text)
    return ClipboardWriteResult(characters_written=len(arguments.text))


def _copy_selected_text(
    arguments: FocusedWindowArguments,
    context: ToolContext,
) -> CopySelectedTextResult:
    """Copy selected text while reliably detecting when nothing was copied."""

    del context
    _require_focused_window(arguments.target_window)
    previous = pyperclip.paste()
    sentinel = f"__WYZER_CLIPBOARD_SENTINEL_{uuid.uuid4().hex}__"
    pyperclip.copy(sentinel)

    try:
        _send_ctrl_shortcut(_VK_C)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            current = pyperclip.paste()
            if current != sentinel:
                return CopySelectedTextResult(
                    copied=True,
                    text=current,
                    message="The selected text was copied to the clipboard.",
                )
            time.sleep(0.05)
    except Exception:
        pyperclip.copy(previous)
        raise

    pyperclip.copy(previous)
    return CopySelectedTextResult(
        copied=False,
        text=None,
        message=(
            "Nothing was copied. Make sure text is highlighted and the application "
            "containing it is still focused."
        ),
    )


def _paste_clipboard(
    arguments: FocusedWindowArguments,
    context: ToolContext,
) -> PasteClipboardResult:
    del context
    _require_focused_window(arguments.target_window)
    _send_ctrl_shortcut(_VK_V)
    return PasteClipboardResult(
        pasted=True,
        message="The clipboard was pasted into the focused application.",
    )


def _make_read_tool() -> CallableTool[NoArguments, ClipboardResult]:
    return CallableTool(
        name="read_clipboard",
        description="Read the current plain-text clipboard contents.",
        arguments_type=NoArguments,
        result_type=ClipboardResult,
        handler=_read_clipboard,
        risk_level=RiskLevel.LOW,
        read_only=True,
    )


def _make_write_tool() -> CallableTool[WriteClipboardArguments, ClipboardWriteResult]:
    return CallableTool(
        name="write_clipboard",
        description=(
            "Replace the clipboard with exact text explicitly supplied by the user. "
            "Do not use this for highlighted or selected text; use copy_selected_text."
        ),
        arguments_type=WriteClipboardArguments,
        result_type=ClipboardWriteResult,
        handler=_write_clipboard,
        risk_level=RiskLevel.MEDIUM,
        read_only=False,
    )


def _make_copy_selected_tool() -> CallableTool[FocusedWindowArguments, CopySelectedTextResult]:
    return CallableTool(
        name="copy_selected_text",
        description=(
            "Copy the currently highlighted or selected text from the focused Windows "
            "application by sending Ctrl+C. Use when the user says highlighted text, "
            "selected text, current selection, or copy this while text is selected."
        ),
        arguments_type=FocusedWindowArguments,
        result_type=CopySelectedTextResult,
        handler=_copy_selected_text,
        risk_level=RiskLevel.LOW,
        read_only=False,
    )


def _make_paste_tool() -> CallableTool[FocusedWindowArguments, PasteClipboardResult]:
    return CallableTool(
        name="paste_clipboard",
        description=(
            "Paste the current clipboard into the focused Windows application by "
            "sending Ctrl+V. Use when the user asks to paste, paste it, or insert the "
            "clipboard at the current cursor position."
        ),
        arguments_type=FocusedWindowArguments,
        result_type=PasteClipboardResult,
        handler=_paste_clipboard,
        risk_level=RiskLevel.MEDIUM,
        read_only=False,
    )


def create_clipboard_pack() -> SimpleToolPack:
    return SimpleToolPack(
        name="clipboard",
        tool_factories=(
            _make_read_tool,
            _make_write_tool,
            _make_copy_selected_tool,
            _make_paste_tool,
        ),
    )


# Backward-friendly module-level alias for pack-oriented tests/extensions.
create_pack = create_clipboard_pack
