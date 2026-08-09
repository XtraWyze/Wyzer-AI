"""Local Faster-Whisper speech recognition with CUDA-to-CPU fallback."""

from __future__ import annotations

import importlib
import math
import os
import tempfile
import warnings
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from wyzer.speech.windows import SpeechAdapterError, SpeechDiagnostic, SpeechRecognition


class WhisperModelProtocol(Protocol):
    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]: ...


class FasterWhisperRecognizer:
    sample_rate = 16_000

    def __init__(
        self,
        model_name: str = "small.en",
        *,
        device: str = "auto",
        compute_type: str = "int8_float16",
        download_root: Path = Path(".wyzer/models"),
        minimum_confidence: float = 0.35,
        capture_utterance: Callable[[float], bytes | None],
        model_factory: Callable[..., WhisperModelProtocol] | None = None,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self.compute_type = compute_type
        self.download_root = download_root.expanduser().resolve()
        self.minimum_confidence = minimum_confidence
        self._capture = capture_utterance
        self._model_factory = model_factory
        self._model: WhisperModelProtocol | None = None
        self.active_device: str | None = None

    def diagnose(self) -> SpeechDiagnostic:
        try:
            self._ensure_model()
        except SpeechAdapterError as error:
            return SpeechDiagnostic(False, str(error))
        return SpeechDiagnostic(
            True,
            f"Faster-Whisper {self.model_name} is ready on {self.active_device}.",
        )

    def listen(self, timeout_seconds: float = 8) -> SpeechRecognition | None:
        audio = self._capture(timeout_seconds)
        if not audio:
            return None
        handle, raw_path = tempfile.mkstemp(prefix="wyzer-whisper-", suffix=".wav")
        os.close(handle)
        path = Path(raw_path)
        try:
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(self.sample_rate)
                output.writeframes(audio)
            try:
                return self._transcribe(path)
            except Exception as error:
                if self.active_device != "cuda":
                    raise SpeechAdapterError(f"Whisper transcription failed: {error}") from error
                self._model = None
                self.active_device = None
                self.requested_device = "cpu"
                self.compute_type = "int8"
                try:
                    return self._transcribe(path)
                except Exception as fallback_error:
                    raise SpeechAdapterError(
                        f"Whisper failed on CUDA and CPU: {fallback_error}"
                    ) from fallback_error
        finally:
            path.unlink(missing_ok=True)

    def _transcribe(self, path: Path) -> SpeechRecognition | None:
        model = self._ensure_model()
        segments_raw, _ = model.transcribe(
            str(path),
            language="en",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            hotwords=(
                "Wyzer WyzerNext File Explorer Rocket League Epic Games Launcher "
                "open close minimize maximize pause play screen OCR monitor folder"
            ),
        )
        segments = [
            segment
            for segment in segments_raw
            if float(getattr(segment, "no_speech_prob", 0.0)) < 0.6
        ]
        text = " ".join(str(segment.text).strip() for segment in segments).strip()
        if not text:
            return None
        log_probs = [float(segment.avg_logprob) for segment in segments]
        confidence = math.exp(sum(log_probs) / len(log_probs)) if log_probs else 0.0
        confidence = max(0.0, min(1.0, confidence))
        if confidence < self.minimum_confidence:
            return None
        return SpeechRecognition(text=text, confidence=confidence)

    def _ensure_model(self) -> WhisperModelProtocol:
        if self._model is not None:
            return self._model
        self.download_root.mkdir(parents=True, exist_ok=True)
        device = self._select_device()
        compute = self.compute_type if device == "cuda" else "int8"
        try:
            factory = self._model_factory or self._default_factory()
            self._model = factory(
                self.model_name,
                device=device,
                compute_type=compute,
                download_root=str(self.download_root),
                local_files_only=True,
            )
        except Exception as error:
            if device == "cuda" and self.requested_device == "auto":
                try:
                    factory = self._model_factory or self._default_factory()
                    self._model = factory(
                        self.model_name,
                        device="cpu",
                        compute_type="int8",
                        download_root=str(self.download_root),
                        local_files_only=True,
                    )
                    device = "cpu"
                except Exception as fallback_error:
                    raise SpeechAdapterError(
                        f"Faster-Whisper model could not load: {fallback_error}"
                    ) from fallback_error
            else:
                raise SpeechAdapterError(f"Faster-Whisper model could not load: {error}") from error
        self.active_device = device
        return self._model

    def _select_device(self) -> str:
        if self.requested_device in {"cpu", "cuda"}:
            return self.requested_device
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"urllib3 .* doesn't match a supported version!",
                    module=r"requests(\..*)?",
                )
                ctranslate2 = importlib.import_module("ctranslate2")
            return "cuda" if int(ctranslate2.get_cuda_device_count()) > 0 else "cpu"
        except Exception:
            return "cpu"

    @staticmethod
    def _default_factory() -> Callable[..., WhisperModelProtocol]:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"urllib3 .* doesn't match a supported version!",
                    module=r"requests(\..*)?",
                )
                module = importlib.import_module("faster_whisper")
            return cast(Callable[..., WhisperModelProtocol], module.WhisperModel)
        except ImportError as error:
            raise SpeechAdapterError("Faster-Whisper is not installed.") from error
