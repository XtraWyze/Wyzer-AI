from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from wyzer.brain import FakeChatProvider
from wyzer.config import WyzerSettings
from wyzer.files import IndexStats
from wyzer.ui import desktop


class _World:
    def set_operating_mode(self, mode: str) -> None:
        del mode


class _Assistant:
    def __init__(self) -> None:
        self.world = _World()
        self.active = 0
        self.maximum_active = 0
        self.interrupted = False

    def set_progress_callback(self, callback: Any) -> None:
        del callback

    async def handle(self, text: str) -> Any:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        for _ in range(30):
            await asyncio.sleep(0.005)
            if self.interrupted:
                break
        self.active -= 1
        return SimpleNamespace(
            text="Okay, I stopped it." if self.interrupted else f"Done: {text}",
            interrupted=self.interrupted,
        )

    def interrupt(self) -> bool:
        was_active = self.active > 0
        self.interrupted = True
        return was_active


class _Speaker:
    def stop(self) -> None:
        return None


@pytest.fixture
def qt_app() -> QApplication:
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])


def _wait_for(app: QApplication, predicate: Any, *, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the desktop runtime")


def _runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[desktop.AssistantRuntime, _Assistant]:
    monkeypatch.setattr(desktop, "create_speech_synthesizer", lambda settings: _Speaker())
    monkeypatch.setattr(
        desktop, "run_startup_quick_scan", lambda: IndexStats(3, 0, 0, 0)
    )
    assistant = _Assistant()
    runtime = desktop.AssistantRuntime(
        assistant,
        FakeChatProvider(available=False),
        WyzerSettings(),
        voice_enabled=False,
        wake_phrase="hey wyzer",
    )
    return runtime, assistant


def test_desktop_runtime_serializes_rapid_submissions(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, assistant = _runtime(monkeypatch)
    replies: list[str] = []
    statuses: list[str] = []
    runtime.replied.connect(replies.append)
    runtime.status_changed.connect(statuses.append)
    runtime.start()
    try:
        _wait_for(qt_app, lambda: runtime._loop is not None)
        runtime.submit("one")
        runtime.submit("two")
        _wait_for(
            qt_app,
            lambda: len(replies) == 2 and bool(statuses) and statuses[-1] == "Idle",
        )

        assert assistant.maximum_active == 1
        assert replies == ["Done: one", "Done: two"]
        assert statuses[-1] == "Idle"
    finally:
        runtime.shutdown()


def test_desktop_stop_does_not_repeat_acknowledgement(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, assistant = _runtime(monkeypatch)
    replies: list[str] = []
    runtime.replied.connect(replies.append)
    runtime.start()
    try:
        _wait_for(qt_app, lambda: runtime._loop is not None)
        runtime.submit("long task")
        _wait_for(qt_app, lambda: assistant.active == 1)
        runtime.stop_current()
        _wait_for(qt_app, lambda: assistant.active == 0)

        assert replies == ["Okay, I stopped it."]
    finally:
        runtime.shutdown()


def test_tray_actions_use_personalized_assistant_name(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _assistant = _runtime(monkeypatch)
    monkeypatch.setattr(desktop.QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    companion = desktop.DesktopCompanion(qt_app, runtime, "Nova")

    assert companion._tray_menu is not None
    labels = [action.text() for action in companion._tray_menu.actions()]
    assert "Chat with Nova" in labels
    assert "Quit Nova" in labels
