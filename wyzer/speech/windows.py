"""Offline Windows speech adapters backed by the installed System.Speech engine."""

from __future__ import annotations

import importlib
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SpeechAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechRecognition:
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SpeechDiagnostic:
    available: bool
    message: str
    recognizers: tuple[str, ...] = ()
    voices: tuple[str, ...] = ()


class SpeechCommandRunner(Protocol):
    def run(
        self, script: str, *, payload: dict[str, Any] | None = None, timeout: float = 15
    ) -> str: ...


class PowerShellSpeechRunner:
    def __init__(self) -> None:
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        self.executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        self._process_lock = threading.RLock()
        self._active_processes: set[subprocess.Popen[str]] = set()

    def run(
        self, script: str, *, payload: dict[str, Any] | None = None, timeout: float = 15
    ) -> str:
        return self._run(script, payload=payload, timeout=timeout, cancel_event=None)

    def run_cancellable(
        self,
        script: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
        cancel_event: threading.Event,
    ) -> str:
        return self._run(
            script,
            payload=payload,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def _run(
        self,
        script: str,
        *,
        payload: dict[str, Any] | None,
        timeout: float,
        cancel_event: threading.Event | None,
    ) -> str:
        if platform.system() != "Windows" or not self.executable.is_file():
            raise SpeechAdapterError("Windows PowerShell speech support is unavailable.")
        if cancel_event is not None and cancel_event.is_set():
            return ""
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [
                    str(self.executable),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._process_lock:
                self._active_processes.add(process)
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
            stdout, stderr = process.communicate(
                input=json.dumps(payload or {}),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            if process is not None:
                process.kill()
                process.communicate()
            raise SpeechAdapterError(f"Windows speech command failed: {error}") from error
        except OSError as error:
            raise SpeechAdapterError(f"Windows speech command failed: {error}") from error
        finally:
            if process is not None:
                with self._process_lock:
                    self._active_processes.discard(process)
        if cancel_event is not None and cancel_event.is_set():
            return ""
        if process.returncode != 0:
            detail = stderr.strip().splitlines()
            message = detail[-1] if detail else "Windows speech returned an error."
            raise SpeechAdapterError(message)
        return stdout.strip()

    def cancel_active(self) -> None:
        """Terminate speech commands owned by this runner instance.

        Synthesizers and short-lived control recognizers each own their own runner,
        so cancellation stays scoped instead of killing unrelated PowerShell work.
        """
        with self._process_lock:
            processes = tuple(self._active_processes)
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                process.terminate()
            except OSError:
                continue


_DIAGNOSTIC_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$recognizers = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
    ForEach-Object { $_.Culture.Name + ': ' + $_.Description })
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($speaker.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
@{ recognizers = $recognizers; voices = $voices } | ConvertTo-Json -Compress
"""

_RECOGNIZE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$installed = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
if ($installed.Count -eq 0) { throw 'No Windows speech recognizer is installed.' }
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($installed[0])
$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$engine.SetInputToDefaultAudioDevice()
$result = $engine.Recognize([TimeSpan]::FromSeconds([double]$request.timeout_seconds))
if ($null -eq $result) { @{ heard = $false } | ConvertTo-Json -Compress }
else { @{ heard = $true; text = $result.Text; confidence = $result.Confidence } |
    ConvertTo-Json -Compress }
"""

_TRANSCRIBE_WAV_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$installed = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
if ($installed.Count -eq 0) { throw 'No Windows speech recognizer is installed.' }
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($installed[0])
$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$engine.SetInputToWaveFile([string]$request.audio_path)
$result = $engine.Recognize()
if ($null -eq $result) { @{ heard = $false } | ConvertTo-Json -Compress }
else { @{ heard = $true; text = $result.Text; confidence = $result.Confidence } |
    ConvertTo-Json -Compress }
"""

_WAKE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$installed = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
if ($installed.Count -eq 0) { throw 'No Windows speech recognizer is installed.' }
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($installed[0])
$builder = New-Object System.Speech.Recognition.GrammarBuilder
$builder.Append([string]$request.wake_phrase)
$engine.LoadGrammar((New-Object System.Speech.Recognition.Grammar($builder)))
$engine.SetInputToDefaultAudioDevice()
$result = $engine.Recognize([TimeSpan]::FromSeconds([double]$request.timeout_seconds))
if ($null -eq $result) { @{ heard = $false } | ConvertTo-Json -Compress }
else { @{ heard = $true; text = $result.Text; confidence = $result.Confidence } |
    ConvertTo-Json -Compress }
"""

_PHRASE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$installed = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers())
if ($installed.Count -eq 0) { throw 'No Windows speech recognizer is installed.' }
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($installed[0])
$choices = New-Object System.Speech.Recognition.Choices
$choices.Add([string[]]$request.phrases)
$builder = New-Object System.Speech.Recognition.GrammarBuilder
$builder.Append($choices)
$engine.LoadGrammar((New-Object System.Speech.Recognition.Grammar($builder)))
$engine.SetInputToDefaultAudioDevice()
$result = $engine.Recognize([TimeSpan]::FromSeconds([double]$request.timeout_seconds))
if ($null -eq $result) { @{ heard = $false } | ConvertTo-Json -Compress }
else { @{ heard = $true; text = $result.Text; confidence = $result.Confidence } |
    ConvertTo-Json -Compress }
"""

_SPEAK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($request.voice) { $speaker.SelectVoice([string]$request.voice) }
$speaker.Rate = [int]$request.rate
$speaker.Volume = [int]$request.volume
$speaker.Speak([string]$request.text)
"""


class WindowsSpeechRecognizer:
    sample_rate = 16_000
    frame_samples = 480

    def __init__(
        self,
        runner: SpeechCommandRunner | None = None,
        *,
        minimum_confidence: float = 0.35,
        capture_utterance: Callable[[float], bytes | None] | None = None,
    ) -> None:
        self.runner = runner or PowerShellSpeechRunner()
        self.minimum_confidence = minimum_confidence
        self._capture = capture_utterance or self.capture_utterance

    def listen(self, timeout_seconds: float = 8) -> SpeechRecognition | None:
        audio = self._capture(timeout_seconds)
        if not audio:
            return None
        handle, raw_path = tempfile.mkstemp(prefix="wyzer-speech-", suffix=".wav")
        os.close(handle)
        path = Path(raw_path)
        try:
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(self.sample_rate)
                output.writeframes(audio)
            raw = self.runner.run(
                _TRANSCRIBE_WAV_SCRIPT,
                payload={"audio_path": str(path)},
                timeout=20,
            )
        finally:
            path.unlink(missing_ok=True)
        result = _json_result(raw)
        if not result.get("heard"):
            return None
        text = str(result.get("text") or "").strip()
        confidence = float(result.get("confidence") or 0)
        if not text or confidence < self.minimum_confidence:
            return None
        return SpeechRecognition(text=text, confidence=confidence)

    def capture_utterance(self, timeout_seconds: float) -> bytes | None:
        try:
            numpy = importlib.import_module("numpy")
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as error:
            raise SpeechAdapterError(
                "SoundDevice and NumPy are required for microphone input."
            ) from error
        threshold = 250.0
        silence_frames_required = 25
        pre_roll: deque[bytes] = deque(maxlen=5)
        captured: list[bytes] = []
        speaking = False
        silence_frames = 0
        deadline = time.monotonic() + timeout_seconds
        phrase_deadline: float | None = None
        try:
            with sounddevice.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.frame_samples,
                channels=1,
                dtype="int16",
            ) as stream:
                while time.monotonic() < deadline or speaking:
                    if phrase_deadline is not None and time.monotonic() >= phrase_deadline:
                        break
                    raw, overflowed = stream.read(self.frame_samples)
                    if overflowed:
                        continue
                    frame = bytes(raw)
                    samples = numpy.frombuffer(frame, dtype=numpy.int16).astype(numpy.float32)
                    level = float(numpy.sqrt(numpy.mean(samples * samples)))
                    if not speaking:
                        pre_roll.append(frame)
                        if level < threshold:
                            continue
                        speaking = True
                        phrase_deadline = time.monotonic() + 15
                        captured.extend(pre_roll)
                    else:
                        captured.append(frame)
                    if speaking and level < threshold:
                        silence_frames += 1
                        if silence_frames >= silence_frames_required:
                            break
                    else:
                        silence_frames = 0
        except Exception as error:
            raise SpeechAdapterError(f"Microphone capture failed: {error}") from error
        return b"".join(captured) if captured else None

    def diagnose(self) -> SpeechDiagnostic:
        try:
            result = _json_result(self.runner.run(_DIAGNOSTIC_SCRIPT, timeout=10))
        except SpeechAdapterError as error:
            return SpeechDiagnostic(False, str(error))
        recognizers = tuple(str(item) for item in result.get("recognizers") or ())
        voices = tuple(str(item) for item in result.get("voices") or ())
        available = bool(recognizers and voices)
        message = (
            "Windows speech recognition and synthesis are ready."
            if available
            else "Install a Windows speech recognition language and voice."
        )
        return SpeechDiagnostic(available, message, recognizers, voices)


class WindowsWakeWordDetector:
    def __init__(
        self,
        wake_phrase: str = "hey wyzer",
        runner: SpeechCommandRunner | None = None,
        *,
        minimum_confidence: float = 0.55,
    ) -> None:
        self.wake_phrase = wake_phrase.strip()
        self.runner = runner or PowerShellSpeechRunner()
        self.minimum_confidence = minimum_confidence

    def wait(self, timeout_seconds: float = 30) -> bool:
        raw = self.runner.run(
            _WAKE_SCRIPT,
            payload={"wake_phrase": self.wake_phrase, "timeout_seconds": timeout_seconds},
            timeout=timeout_seconds + 10,
        )
        result = _json_result(raw)
        confidence = float(result.get("confidence") or 0)
        return bool(result.get("heard")) and confidence >= self.minimum_confidence


class WindowsPhraseDetector:
    """Listen for a tiny local control grammar such as Wyzer stop/cancel.

    This deliberately bypasses the LLM and normal command STT.  It exists so a
    user can interrupt an in-flight tool turn or spoken response immediately.
    """

    def __init__(
        self,
        phrases: tuple[str, ...] | list[str],
        runner: SpeechCommandRunner | None = None,
        *,
        minimum_confidence: float = 0.45,
    ) -> None:
        cleaned = tuple(
            dict.fromkeys(" ".join(item.strip().split()) for item in phrases if item.strip())
        )
        if not cleaned:
            raise ValueError("at least one control phrase is required")
        self.phrases = cleaned
        self.runner = runner or PowerShellSpeechRunner()
        self.minimum_confidence = minimum_confidence
        self._cancelled = threading.Event()

    def recognize(self, timeout_seconds: float = 120) -> SpeechRecognition | None:
        if self._cancelled.is_set():
            return None
        try:
            payload = {
                "phrases": list(self.phrases),
                "timeout_seconds": timeout_seconds,
            }
            cancellable = getattr(self.runner, "run_cancellable", None)
            if callable(cancellable):
                raw = cancellable(
                    _PHRASE_SCRIPT,
                    payload=payload,
                    timeout=timeout_seconds + 10,
                    cancel_event=self._cancelled,
                )
            else:
                raw = self.runner.run(
                    _PHRASE_SCRIPT,
                    payload=payload,
                    timeout=timeout_seconds + 10,
                )
        except SpeechAdapterError:
            if self._cancelled.is_set():
                return None
            raise
        if self._cancelled.is_set() or not raw:
            return None
        result = _json_result(raw)
        confidence = float(result.get("confidence") or 0)
        text = str(result.get("text") or "").strip()
        if not result.get("heard") or not text or confidence < self.minimum_confidence:
            return None
        return SpeechRecognition(text=text, confidence=confidence)

    def wait(self, timeout_seconds: float = 120) -> bool:
        return self.recognize(timeout_seconds) is not None

    def cancel(self) -> None:
        self._cancelled.set()
        cancel = getattr(self.runner, "cancel_active", None)
        if callable(cancel):
            cancel()


class WindowsSpeechSynthesizer:
    def __init__(
        self,
        runner: SpeechCommandRunner | None = None,
        *,
        voice: str | None = None,
        rate: int = 0,
        volume: int = 100,
    ) -> None:
        self.runner = runner or PowerShellSpeechRunner()
        self.voice = voice
        self.rate = max(-10, min(10, rate))
        self.volume = max(0, min(100, volume))
        self._stop_requested = threading.Event()

    def warm_up(self) -> None:
        """System.Speech has no persistent model to preload."""

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        self._stop_requested.clear()
        try:
            payload = {
                "text": text[:10_000],
                "voice": self.voice,
                "rate": self.rate,
                "volume": self.volume,
            }
            cancellable = getattr(self.runner, "run_cancellable", None)
            if callable(cancellable):
                cancellable(
                    _SPEAK_SCRIPT,
                    payload=payload,
                    timeout=max(15, min(120, len(text) / 8)),
                    cancel_event=self._stop_requested,
                )
            else:
                self.runner.run(
                    _SPEAK_SCRIPT,
                    payload=payload,
                    timeout=max(15, min(120, len(text) / 8)),
                )
        except SpeechAdapterError:
            if self._stop_requested.is_set():
                return
            raise

    def stop(self) -> None:
        self._stop_requested.set()
        cancel = getattr(self.runner, "cancel_active", None)
        if callable(cancel):
            cancel()


def _json_result(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SpeechAdapterError("Windows speech returned invalid data.") from error
    if not isinstance(value, dict):
        raise SpeechAdapterError("Windows speech returned an invalid result.")
    return value
