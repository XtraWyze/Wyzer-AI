"""TTS adapter selection."""

from __future__ import annotations

from typing import Protocol

from wyzer.config import SpeechSettings
from wyzer.speech.kokoro import KokoroSpeechSynthesizer
from wyzer.speech.windows import SpeechAdapterError, WindowsSpeechSynthesizer


class SpeechSynthesizer(Protocol):
    def warm_up(self) -> None: ...

    def speak(self, text: str) -> None: ...

    def stop(self) -> None: ...


def create_speech_synthesizer(settings: SpeechSettings) -> SpeechSynthesizer:
    adapter = settings.tts_adapter.strip().casefold().replace("-", "_")
    if adapter in {"kokoro", "kokoro_tts"}:
        return KokoroSpeechSynthesizer(
            voice=settings.voice or "af_heart",
            speed=settings.tts_speed,
            volume=settings.volume,
            device=settings.tts_device,
        )
    if adapter in {"windows", "windows_system", "system_speech"}:
        return WindowsSpeechSynthesizer(
            voice=settings.voice,
            rate=settings.rate,
            volume=settings.volume,
        )
    raise SpeechAdapterError(f"Unsupported TTS adapter: {settings.tts_adapter}")
