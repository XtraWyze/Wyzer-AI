"""OpenWakeWord microphone detector for user-supplied ONNX wake models."""

from __future__ import annotations

import contextlib
import importlib
import time
import warnings
from pathlib import Path
from typing import Any, Protocol, cast

from wyzer.speech.windows import SpeechAdapterError


class WakeModel(Protocol):
    def predict(self, audio: Any) -> dict[str, Any]: ...

    def reset(self) -> None: ...


class AudioInput(Protocol):
    def __enter__(self) -> AudioInput: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self, frames: int) -> tuple[Any, bool]: ...


class OpenWakeWordDetector:
    sample_rate = 16_000
    frame_samples = 1_280

    def __init__(
        self,
        model_path: Path,
        *,
        threshold: float = 0.55,
        model: WakeModel | None = None,
        audio_factory: Any | None = None,
    ) -> None:
        self.model_path = model_path.expanduser().resolve()
        if not self.model_path.is_file() or self.model_path.suffix.casefold() != ".onnx":
            raise SpeechAdapterError(f"Wake-word ONNX model was not found: {self.model_path}")
        self.threshold = threshold
        self._model = model or self._load_model()
        self._audio_factory = audio_factory or self._default_audio
        self._audio_context: AudioInput | None = None
        self._audio_stream: AudioInput | None = None

    def _load_model(self) -> WakeModel:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"urllib3 .* doesn't match a supported version!",
                    module=r"requests(\..*)?",
                )
                module = importlib.import_module("openwakeword.model")
            return cast(
                WakeModel,
                module.Model(wakeword_models=[str(self.model_path)], inference_framework="onnx"),
            )
        except Exception as error:
            raise SpeechAdapterError(f"OpenWakeWord could not load the model: {error}") from error

    @staticmethod
    def _default_audio() -> AudioInput:
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as error:
            raise SpeechAdapterError(
                "The sounddevice microphone adapter is not installed."
            ) from error
        return cast(
            AudioInput,
            sounddevice.RawInputStream(
                samplerate=OpenWakeWordDetector.sample_rate,
                blocksize=OpenWakeWordDetector.frame_samples,
                channels=1,
                dtype="int16",
            ),
        )

    def _ensure_audio_stream(self) -> AudioInput:
        if self._audio_stream is not None:
            return self._audio_stream
        context = self._audio_factory()
        try:
            stream = context.__enter__()
        except Exception:
            with contextlib.suppress(Exception):
                context.__exit__(None, None, None)
            raise
        self._audio_context = context
        self._audio_stream = stream
        return stream

    def close(self) -> None:
        """Release the wake-word microphone stream, if one is open."""
        context = self._audio_context
        self._audio_context = None
        self._audio_stream = None
        if context is None:
            return
        # Cleanup must not mask the speech error or shutdown path that led here.
        with contextlib.suppress(Exception):
            context.__exit__(None, None, None)

    def wait(self, timeout_seconds: float = 30) -> bool:
        try:
            numpy = importlib.import_module("numpy")
        except ImportError as error:
            raise SpeechAdapterError("NumPy is required for OpenWakeWord audio.") from error
        # Windows' monotonic clock can resolve to only 15.625 ms. Voice mode
        # polls with short deadlines, so use the high-resolution performance
        # counter to avoid accepting frames that arrived after the timeout.
        deadline = time.perf_counter() + timeout_seconds
        self._model.reset()
        try:
            # Keep the microphone open across ordinary idle timeouts.  UI mode polls the
            # detector frequently so it can shut down promptly; reopening PortAudio on
            # every poll can eventually strand a Windows microphone stream.  The stream
            # is released as soon as the wake word is detected so command STT can own it.
            stream = self._ensure_audio_stream()
            while time.perf_counter() < deadline:
                raw, overflowed = stream.read(self.frame_samples)
                # PortAudio reads are blocking.  Never accept a detection from a
                # frame that arrived after the caller's deadline.
                if time.perf_counter() >= deadline:
                    return False
                if overflowed:
                    continue
                audio = numpy.frombuffer(raw, dtype=numpy.int16)
                scores = self._model.predict(audio)
                if any(_latest_score(value) >= self.threshold for value in scores.values()):
                    self.close()
                    return True
        except SpeechAdapterError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise SpeechAdapterError(f"Wake-word microphone detection failed: {error}") from error
        return False


def find_wake_model(directory: Path, preferred: str | None = None) -> Path:
    root = directory.expanduser().resolve()
    if preferred:
        candidate = root / preferred
        if candidate.is_file():
            return candidate
    models = sorted(root.glob("*.onnx"), key=lambda item: item.name.casefold())
    if not models:
        raise SpeechAdapterError(f"No ONNX wake-word models were found in {root}.")
    wyzer = next((item for item in models if "wyzer" in item.stem.casefold()), None)
    return wyzer or models[0]


def _latest_score(value: Any) -> float:
    try:
        if hasattr(value, "reshape"):
            flattened = value.reshape(-1)
            return float(flattened[-1]) if len(flattened) else 0.0
        if isinstance(value, (list, tuple)):
            return float(value[-1]) if value else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
