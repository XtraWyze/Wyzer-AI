from __future__ import annotations

from typing import Any

from wyzer.config import SpeechSettings
from wyzer.speech.kokoro import KokoroSpeechSynthesizer, language_code_for_voice
from wyzer.speech.tts import create_speech_synthesizer
from wyzer.speech.windows import WindowsSpeechSynthesizer


def test_kokoro_voice_infers_language() -> None:
    assert language_code_for_voice("af_heart") == "a"
    assert language_code_for_voice("jf_alpha") == "j"
    assert language_code_for_voice("bf_emma") == "b"


def test_factory_selects_kokoro() -> None:
    speaker = create_speech_synthesizer(
        SpeechSettings(tts_adapter="kokoro", voice="af_heart", tts_speed=1.12)
    )
    assert isinstance(speaker, KokoroSpeechSynthesizer)
    assert speaker.voice == "af_heart"
    assert speaker.speed == 1.12


def test_factory_preserves_windows_adapter() -> None:
    speaker = create_speech_synthesizer(SpeechSettings(tts_adapter="windows_system"))
    assert isinstance(speaker, WindowsSpeechSynthesizer)


def test_kokoro_warm_up_loads_voice_without_playback(monkeypatch: Any) -> None:
    calls: list[tuple[str, str]] = []

    class Pipeline:
        def __init__(self, *, lang_code: str, device: str) -> None:
            calls.append((lang_code, device))

        def __call__(self, text: str, *, voice: str, **_kwargs: Any):
            calls.append((text, voice))
            yield text, "phonemes", [0.0]

    class KokoroModule:
        KPipeline = Pipeline

    import wyzer.speech.kokoro as kokoro_module

    real_import = kokoro_module.importlib.import_module

    def fake_import(name: str):
        return KokoroModule if name == "kokoro" else real_import(name)

    monkeypatch.setattr(kokoro_module.importlib, "import_module", fake_import)
    speaker = KokoroSpeechSynthesizer(voice="af_heart", device="cpu")

    speaker.warm_up()

    assert calls == [("a", "cpu"), ("Ready.", "af_heart")]
