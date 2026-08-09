"""Local Kokoro TTS adapter with an explicit startup warm-up path."""

from __future__ import annotations

import importlib
import threading
from typing import Any

from wyzer.speech.windows import SpeechAdapterError

_VOICE_LANGUAGE_PREFIXES = {
    "af": "a",  # American English female
    "am": "a",  # American English male
    "bf": "b",  # British English female
    "bm": "b",  # British English male
    "ef": "e",  # Spanish female
    "em": "e",  # Spanish male
    "ff": "f",  # French female
    "hf": "h",  # Hindi female
    "hm": "h",  # Hindi male
    "if": "i",  # Italian female
    "im": "i",  # Italian male
    "jf": "j",  # Japanese female
    "jm": "j",  # Japanese male
    "pf": "p",  # Brazilian Portuguese female
    "pm": "p",  # Brazilian Portuguese male
    "zf": "z",  # Mandarin female
    "zm": "z",  # Mandarin male
}


def language_code_for_voice(voice: str) -> str:
    """Infer Kokoro's language code from a normal voice name such as af_heart."""
    return _VOICE_LANGUAGE_PREFIXES.get(voice.strip().casefold()[:2], "a")


class KokoroSpeechSynthesizer:
    """Reusable local Kokoro speech synthesizer with direct SoundDevice playback."""

    sample_rate = 24_000

    def __init__(
        self,
        *,
        voice: str | None = None,
        speed: float = 1.08,
        volume: int = 100,
        device: str = "cpu",
        language_code: str | None = None,
    ) -> None:
        self.voice = (voice or "af_heart").strip()
        self.speed = max(0.5, min(2.0, float(speed)))
        self.volume = max(0, min(100, int(volume)))
        self.device = device.strip().casefold() or "cpu"
        self.language_code = language_code or language_code_for_voice(self.voice)
        self._pipeline: Any | None = None
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()

    def _resolve_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            torch = importlib.import_module("torch")
            return "cuda" if bool(torch.cuda.is_available()) else "cpu"
        except ImportError:
            return "cpu"

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            kokoro = importlib.import_module("kokoro")
        except ImportError as error:
            raise SpeechAdapterError(
                "Kokoro TTS is not installed. Install it with: pip install kokoro"
            ) from error
        pipeline_type = getattr(kokoro, "KPipeline", None)
        if pipeline_type is None:
            raise SpeechAdapterError("The installed kokoro package does not expose KPipeline.")
        device = self._resolve_device()
        try:
            # Current Kokoro supports an explicit device. Keep a compatibility
            # fallback for older installations whose constructor lacks it.
            try:
                self._pipeline = pipeline_type(
                    lang_code=self.language_code,
                    device=device,
                )
            except TypeError:
                self._pipeline = pipeline_type(lang_code=self.language_code)
        except Exception as error:
            raise SpeechAdapterError(f"Kokoro TTS failed to initialize: {error}") from error
        return self._pipeline

    def warm_up(self) -> None:
        """Load the pipeline and selected voice model without playing audio."""
        with self._lock:
            pipeline = self._load_pipeline()
            try:
                generated = pipeline(
                    "Ready.",
                    voice=self.voice,
                    speed=self.speed,
                    split_pattern=r"\n+",
                )
                if next(iter(generated), None) is None:
                    raise SpeechAdapterError("Kokoro returned no warm-up audio.")
            except SpeechAdapterError:
                raise
            except Exception as error:
                raise SpeechAdapterError(f"Kokoro TTS failed to warm up: {error}") from error

    def speak(self, text: str) -> None:
        spoken = text.strip()
        if not spoken or self.volume <= 0:
            return
        self._stop_requested.clear()
        try:
            numpy = importlib.import_module("numpy")
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as error:
            raise SpeechAdapterError("Kokoro playback requires NumPy and SoundDevice.") from error

        with self._lock:
            pipeline = self._load_pipeline()
            try:
                generator = pipeline(
                    spoken[:10_000],
                    voice=self.voice,
                    speed=self.speed,
                    split_pattern=r"\n+",
                )
                gain = self.volume / 100.0
                produced_audio = False
                for _graphemes, _phonemes, audio in generator:
                    if self._stop_requested.is_set():
                        return
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    samples = numpy.asarray(audio, dtype=numpy.float32).reshape(-1)
                    if not samples.size:
                        continue
                    produced_audio = True
                    if gain != 1.0:
                        samples = numpy.clip(samples * gain, -1.0, 1.0)
                    sounddevice.play(samples, samplerate=self.sample_rate)
                    sounddevice.wait()
                    if self._stop_requested.is_set():
                        return
                if not produced_audio:
                    raise SpeechAdapterError("Kokoro returned no audio.")
            except SpeechAdapterError:
                raise
            except Exception as error:
                if self._stop_requested.is_set():
                    return
                raise SpeechAdapterError(f"Kokoro TTS failed: {error}") from error

    def stop(self) -> None:
        """Immediately stop active SoundDevice playback."""
        self._stop_requested.set()
        try:
            sounddevice = importlib.import_module("sounddevice")
            sounddevice.stop()
        except Exception:
            # Stop is best-effort and must remain safe from any caller thread.
            pass
