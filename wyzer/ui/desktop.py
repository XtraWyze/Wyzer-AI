"""Desktop companion runtime for Wyzer.

The Qt window is deliberately a presentation layer. User requests still enter the existing
Orchestrator, which remains responsible for LLM-driven reasoning and native tool selection.
"""

from __future__ import annotations

import asyncio
import random
import re
import threading
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from wyzer.app.orchestrator import Orchestrator
from wyzer.brain import ChatProvider, diagnostic_provider
from wyzer.config import WyzerSettings
from wyzer.runtime_paths import data_home
from wyzer.speech import (
    FasterWhisperRecognizer,
    OpenWakeWordDetector,
    WindowsPhraseDetector,
    WindowsSpeechRecognizer,
    WindowsWakeWordDetector,
    create_speech_synthesizer,
    find_wake_model,
)
from wyzer.speech.text import normalize_spoken_command, speech_safe_text
from wyzer.speech.windows import SpeechAdapterError
from wyzer.ui.character import WyzerCharacter
from wyzer.ui.chat_window import ChatWindow

_AMBIENT_COMMENTS = (
    "I'm here if you need me.",
    "Still hanging around.",
    "Need anything?",
    "I'll stay out of the way.",
    "Just keeping an eye on things.",
    "You can double-click me to type something.",
)

_STOP_COMMAND = re.compile(
    r"^\s*(?:stop|cancel|interrupt|never mind)\s*[.!]?\s*$",
    re.I,
)


class AssistantRuntime(QObject):
    """Own a single asyncio loop so voice and UI text share one Orchestrator safely."""

    status_changed = Signal(str)
    heard = Signal(str)
    replied = Signal(str)
    error = Signal(str)
    ready = Signal(str)

    def __init__(
        self,
        assistant: Orchestrator,
        provider: ChatProvider,
        settings: WyzerSettings,
        *,
        voice_enabled: bool,
        wake_phrase: str,
    ) -> None:
        super().__init__()
        self.assistant = assistant
        self.provider = provider
        self.settings = settings
        self.voice_enabled = voice_enabled
        self.wake_phrase = wake_phrase
        self._muted = False
        self._stopping = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._voice_task: asyncio.Task[Any] | None = None
        self._startup_task: asyncio.Task[Any] | None = None
        self._manual_listen_lock = threading.Lock()
        self._speaker = create_speech_synthesizer(settings.speech)
        self._speaking = threading.Event()
        self._speech_generation = 0
        self._stop_acknowledgement_generation = 0
        self._request_lock: asyncio.Lock | None = None
        self._interrupt_warning_emitted = False
        self.assistant.set_progress_callback(self.status_changed.emit)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="wyzer-ui-loop")
        self._thread.start()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._startup_task = loop.create_task(self._startup())
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _startup(self) -> None:
        self.assistant.world.set_operating_mode("voice" if self.voice_enabled else "text")
        diagnosable = diagnostic_provider(self.provider)
        if diagnosable is not None:
            try:
                diagnostic = await diagnosable.diagnose()
                status = "ready" if diagnostic.available else "unavailable"
                self.ready.emit(f"Local model: {status} - {diagnostic.message}")
                if self.voice_enabled and diagnostic.available:
                    self.status_changed.emit("Loading language model")
                    await diagnosable.warm_up()
                    self.ready.emit("Language model loaded and ready.")
            except Exception as exc:  # UI should stay alive even when diagnostics fail.
                self.error.emit(f"Model diagnostic failed: {exc}")
        if self.voice_enabled:
            self.status_changed.emit("Loading speech model")
            try:
                await asyncio.to_thread(self._speaker.warm_up)
            except SpeechAdapterError as exc:
                self.error.emit(f"Speech output unavailable: {exc}")
                self.status_changed.emit("Idle")
                return
            self.ready.emit("Speech model loaded and ready.")
            self._voice_task = asyncio.create_task(self._voice_loop())
        self.status_changed.emit("Idle")

    def submit(self, text: str, *, speak_reply: bool = False) -> None:
        if _STOP_COMMAND.fullmatch(text):
            self.stop_current()
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            self.error.emit("Wyzer runtime is not ready yet.")
            return
        asyncio.run_coroutine_threadsafe(self._handle(text, speak_reply=speak_reply), loop)

    async def _handle(self, text: str, *, speak_reply: bool) -> None:
        # Text chat and voice can both submit work to this runtime.  Keep the
        # presentation state single-flight so a queued request cannot make the
        # UI appear idle while another request is still running.
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        async with self._request_lock:
            await self._handle_one(text, speak_reply=speak_reply)

    async def _handle_one(self, text: str, *, speak_reply: bool) -> None:
        speech_generation = self._speech_generation
        stop_acknowledgement_generation = self._stop_acknowledgement_generation
        self.status_changed.emit("Thinking")
        action = asyncio.create_task(self.assistant.handle(text))
        detector = self._build_interrupt_detector(allow_bare=True) if speak_reply else None
        interrupt_task = (
            asyncio.create_task(self._wait_for_voice_interrupt(detector))
            if detector is not None
            else None
        )
        pause_response = None
        try:
            if interrupt_task is None:
                response = await action
            else:
                done, _ = await asyncio.wait(
                    {action, interrupt_task}, return_when=asyncio.FIRST_COMPLETED
                )
                control = interrupt_task.result() if interrupt_task in done else None
                if control is not None and re.search(r"\bpause\b", control, re.I):
                    candidate = await self.assistant.handle("pause")
                    if candidate.interrupted:
                        pause_response = candidate
                elif control is not None:
                    self._speech_generation += 1
                    self._speaker.stop()
                    self.assistant.interrupt()
                response = await action
        except Exception as exc:
            self.status_changed.emit("Error")
            self.error.emit(str(exc))
            return
        finally:
            if detector is not None:
                detector.cancel()
            if interrupt_task is not None and not interrupt_task.done():
                await interrupt_task
        if pause_response is not None:
            self.replied.emit(pause_response.text)
            self.status_changed.emit("Idle")
            return
        if (
            response.interrupted
            and stop_acknowledgement_generation != self._stop_acknowledgement_generation
        ):
            # stop_current() already gave the user immediate feedback.  The
            # Orchestrator returns the same acknowledgement as it unwinds.
            self.status_changed.emit("Idle")
            return
        self.replied.emit(response.text)
        if speak_reply and not self._muted and speech_generation == self._speech_generation:
            await self._speak(response.text)
        self.status_changed.emit("Idle")

    @Slot()
    def stop_current(self) -> None:
        speech_stopped = self._speaking.is_set()
        # TTS cancellation is thread-safe and should happen before the asyncio
        # loop round-trip so the user hears the response stop immediately.
        self._speaker.stop()
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._stop_current_on_loop, speech_stopped)

    def _stop_current_on_loop(self, speech_stopped: bool = False) -> None:
        self._speech_generation += 1
        stopped = self.assistant.interrupt() or speech_stopped
        if stopped:
            self._stop_acknowledgement_generation += 1
        self.replied.emit("Okay, I stopped it." if stopped else "There is no active task.")
        self.status_changed.emit("Idle")

    @Slot(bool)
    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    @Slot()
    def listen_once(self) -> None:
        if self.voice_enabled:
            self.replied.emit(f"Say '{self.wake_phrase}' and I'll listen.")
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._manual_listen_once(), loop)

    async def _manual_listen_once(self) -> None:
        if not self._manual_listen_lock.acquire(blocking=False):
            return
        try:
            recognizer = self._build_recognizer()
            self.status_changed.emit("Listening")
            heard = await asyncio.to_thread(
                recognizer.listen, self.settings.speech.listen_timeout_seconds
            )
            if heard is None:
                self.replied.emit("I didn't catch that.")
                return
            self.heard.emit(heard.text)
            await self._handle(normalize_spoken_command(heard.text), speak_reply=True)
        except SpeechAdapterError as exc:
            self.error.emit(f"Speech: {exc}")
        finally:
            self._manual_listen_lock.release()
            self.status_changed.emit("Idle")

    def _build_recognizer(self):
        microphone = WindowsSpeechRecognizer(
            minimum_confidence=self.settings.speech.minimum_stt_confidence
        )
        if self.settings.speech.stt_adapter == "faster_whisper":
            return FasterWhisperRecognizer(
                self.settings.speech.whisper_model,
                device=self.settings.speech.whisper_device,
                compute_type=self.settings.speech.whisper_compute_type,
                download_root=self.settings.speech.whisper_download_root,
                minimum_confidence=self.settings.speech.minimum_stt_confidence,
                capture_utterance=microphone.capture_utterance,
            )
        return microphone

    def _build_wake(self):
        if self.settings.speech.wake_word_adapter == "openwakeword":
            model_path = find_wake_model(
                self.settings.speech.wake_model_directory, self.settings.speech.wake_model
            )
            return OpenWakeWordDetector(
                model_path, threshold=self.settings.speech.minimum_wake_confidence
            )
        return WindowsWakeWordDetector(
            self.wake_phrase,
            minimum_confidence=self.settings.speech.minimum_wake_confidence,
        )

    async def _voice_loop(self) -> None:
        wake = None
        try:
            try:
                recognizer = self._build_recognizer()
                wake = self._build_wake()
                diagnostic = await asyncio.to_thread(recognizer.diagnose)
                if not diagnostic.available:
                    self.error.emit(f"Speech unavailable: {diagnostic.message}")
                    return
                self.ready.emit(f"Voice ready. Say '{self.wake_phrase}'.")
            except SpeechAdapterError as exc:
                self.error.emit(f"Speech unavailable: {exc}")
                return

            while not self._stopping.is_set():
                try:
                    detected = await asyncio.to_thread(
                        wake.wait, min(self.settings.speech.wake_timeout_seconds, 5.0)
                    )
                    if not detected or self._stopping.is_set():
                        continue
                    self.status_changed.emit("Listening")
                    heard = await asyncio.to_thread(
                        recognizer.listen, self.settings.speech.listen_timeout_seconds
                    )
                    if heard is None:
                        self.replied.emit("I didn't catch that.")
                        self.status_changed.emit("Idle")
                        continue
                    self.heard.emit(heard.text)
                    if heard.text.strip().casefold() in {"quit", "exit", "goodbye"}:
                        self.replied.emit("Goodbye.")
                        if not self._muted:
                            await self._speak("Goodbye.")
                        self.status_changed.emit("Idle")
                        continue
                    if _STOP_COMMAND.fullmatch(heard.text):
                        self._stop_current_on_loop()
                        continue
                    await self._handle(normalize_spoken_command(heard.text), speak_reply=True)
                except asyncio.CancelledError:
                    break
                except SpeechAdapterError as exc:
                    self.error.emit(f"Speech error: {exc}")
                    self.status_changed.emit("Idle")
                except Exception as exc:
                    self.error.emit(f"Voice loop error: {exc}")
                    self.status_changed.emit("Idle")
        finally:
            close_wake = getattr(wake, "close", None)
            if callable(close_wake):
                close_wake()

    async def _speak(self, text: str) -> None:
        self._speaking.set()
        detector = self._build_interrupt_detector(allow_bare=False)
        interrupt_task = (
            asyncio.create_task(self._wait_for_voice_interrupt(detector))
            if detector is not None
            else None
        )
        speak_task = asyncio.create_task(
            asyncio.to_thread(self._speaker.speak, speech_safe_text(text))
        )
        try:
            if interrupt_task is None:
                await speak_task
                return
            done, _ = await asyncio.wait(
                {speak_task, interrupt_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if interrupt_task in done and interrupt_task.result() is not None:
                self._speech_generation += 1
                self._speaker.stop()
            await speak_task
        except SpeechAdapterError as exc:
            self.error.emit(f"Speech output error: {exc}")
        finally:
            self._speaking.clear()
            if detector is not None:
                detector.cancel()
            if interrupt_task is not None and not interrupt_task.done():
                await interrupt_task

    def _build_interrupt_detector(self, *, allow_bare: bool) -> WindowsPhraseDetector | None:
        if not self.voice_enabled:
            return None
        wake = " ".join(self.wake_phrase.strip().split())
        phrases = [
            f"{wake} stop",
            f"{wake} cancel",
            "wyzer stop",
            "wyzer cancel",
            "stop wyzer",
            "cancel wyzer",
        ]
        if allow_bare:
            phrases.extend(
                (
                    f"{wake} pause",
                    "wyzer pause",
                    "pause wyzer",
                    "stop",
                    "cancel",
                    "pause",
                    "interrupt",
                    "never mind",
                )
            )
        else:
            phrases.insert(0, wake)
        return WindowsPhraseDetector(
            phrases,
            minimum_confidence=max(0.35, self.settings.speech.minimum_stt_confidence),
        )

    async def _wait_for_voice_interrupt(self, detector: WindowsPhraseDetector) -> str | None:
        try:
            recognized = await asyncio.to_thread(detector.recognize, 300.0)
            return recognized.text if recognized is not None else None
        except SpeechAdapterError as exc:
            if not self._interrupt_warning_emitted:
                self._interrupt_warning_emitted = True
                self.error.emit(f"Voice interrupt unavailable: {exc}")
            return None

    def shutdown(self) -> None:
        self._stopping.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._shutdown_on_loop)

    def _shutdown_on_loop(self) -> None:
        self._speaker.stop()
        self.assistant.interrupt()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.stop()


class DesktopCompanion(QObject):
    def __init__(
        self,
        app: QApplication,
        runtime: AssistantRuntime,
        assistant_name: str,
    ) -> None:
        super().__init__()
        self.app = app
        self.runtime = runtime
        self.character = WyzerCharacter(assistant_name, avatar_dir=data_home() / "avatar")
        self.chat = ChatWindow(assistant_name)
        self._tray_menu: QMenu | None = None
        self._current_status = "Idle"
        self._ambient_timer = QTimer(self)
        self._ambient_timer.timeout.connect(self._ambient_comment)

        self.character.open_chat_requested.connect(self.open_chat)
        self.character.stop_requested.connect(self.runtime.stop_current)
        self.character.listen_requested.connect(self.runtime.listen_once)
        self.character.quit_requested.connect(self.app.quit)
        self.character.muted_changed.connect(self.runtime.set_muted)
        self.character.comments_changed.connect(self._comments_changed)

        self.chat.submitted.connect(self._submit_text)
        self.chat.stop_requested.connect(self.runtime.stop_current)

        self.runtime.status_changed.connect(self._status_changed)
        self.runtime.heard.connect(self._heard)
        self.runtime.replied.connect(self._replied)
        self.runtime.error.connect(self._error)
        self.runtime.ready.connect(self._ready)

        self.tray = self._create_tray(assistant_name)
        self.app.aboutToQuit.connect(self.runtime.shutdown)
        self._schedule_ambient()

    def _create_tray(self, assistant_name: str) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        pixmap = QPixmap(32, 32)
        pixmap.fill("transparent")
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from PySide6.QtGui import QColor

        painter.setBrush(QColor("#68759d"))
        painter.setPen(QColor("#20232d"))
        painter.drawEllipse(3, 3, 26, 26)
        painter.setPen(QColor("white"))
        painter.drawText(pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), "W")
        painter.end()
        tray = QSystemTrayIcon(QIcon(pixmap), self.app)
        tray.setToolTip(assistant_name)
        menu = QMenu()
        show_action = menu.addAction("Show character")
        show_action.triggered.connect(self.character.show)
        chat_action = menu.addAction(f"Chat with {assistant_name}")
        chat_action.triggered.connect(self.open_chat)
        menu.addSeparator()
        stop_action = menu.addAction("Stop current task")
        stop_action.triggered.connect(self.runtime.stop_current)
        menu.addSeparator()
        quit_action = menu.addAction(f"Quit {assistant_name}")
        quit_action.triggered.connect(self.app.quit)
        self._tray_menu = menu
        tray.setContextMenu(menu)
        tray.show()
        return tray

    def start(self) -> None:
        self.character.show()
        self.character.say("I'm here.", 2200)
        self.runtime.start()

    @Slot()
    def open_chat(self) -> None:
        self.chat.show_near_character(self.character)

    @Slot(str)
    def _submit_text(self, text: str) -> None:
        self.runtime.submit(text, speak_reply=False)

    @Slot(str)
    def _status_changed(self, status: str) -> None:
        self._current_status = status
        self.character.set_status(status)
        self.chat.set_status(status)
        if status == "Listening":
            self.character.say("Listening...", 0)
        elif status == "Thinking":
            self.character.say("Thinking...", 0)

    @Slot(str)
    def _heard(self, text: str) -> None:
        self.chat.append_user(text)
        self.character.say(f"You: {text}", 2400)

    @Slot(str)
    def _replied(self, text: str) -> None:
        self.chat.append_assistant(text)
        self.character.say(text, 6500)

    @Slot(str)
    def _error(self, text: str) -> None:
        self.character.set_status("Error")
        self.character.say(text, 6500)
        self.chat.append_assistant(text)

    @Slot(str)
    def _ready(self, text: str) -> None:
        self.character.say(text, 3500)

    @Slot(bool)
    def _comments_changed(self, enabled: bool) -> None:
        if enabled:
            self._schedule_ambient()
        else:
            self._ambient_timer.stop()

    def _schedule_ambient(self) -> None:
        if not self.character.comments_enabled:
            return
        self._ambient_timer.start(random.randint(90_000, 210_000))

    @Slot()
    def _ambient_comment(self) -> None:
        if (
            self.character.comments_enabled
            and self.character.isVisible()
            and self._current_status == "Idle"
        ):
            self.character.say(random.choice(_AMBIENT_COMMENTS), 3500)
        self._schedule_ambient()


def run_desktop_ui(
    assistant: Orchestrator,
    provider: ChatProvider,
    settings: WyzerSettings,
    *,
    app: QApplication | None = None,
    voice_enabled: bool = False,
    wake_phrase: str | None = None,
) -> int:
    """Run the optional desktop companion until the Qt application exits."""
    existing = QApplication.instance()
    resolved_app = app or (existing if isinstance(existing, QApplication) else QApplication([]))
    resolved_app.setQuitOnLastWindowClosed(False)
    runtime = AssistantRuntime(
        assistant,
        provider,
        settings,
        voice_enabled=voice_enabled,
        wake_phrase=wake_phrase or settings.speech.wake_phrase,
    )
    companion = DesktopCompanion(resolved_app, runtime, settings.personality.assistant_name)
    companion.start()
    # Keep the QObject graph alive for the lifetime of QApplication.
    resolved_app._wyzer_companion = companion
    return resolved_app.exec()
