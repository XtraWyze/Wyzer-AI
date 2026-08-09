import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from wyzer.app.cli import console_safe_text
from wyzer.speech import (
    FasterWhisperRecognizer,
    OpenWakeWordDetector,
    WindowsPhraseDetector,
    WindowsSpeechRecognizer,
    WindowsSpeechSynthesizer,
    WindowsWakeWordDetector,
    find_wake_model,
)
from wyzer.speech.text import speech_safe_text


class FakeSpeechRunner:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)
        self.calls: list[dict[str, Any]] = []

    def run(
        self, script: str, *, payload: dict[str, Any] | None = None, timeout: float = 15
    ) -> str:
        self.calls.append({"script": script, "payload": payload, "timeout": timeout})
        return next(self.outputs)


def test_console_output_replaces_unencodable_model_text_without_crashing() -> None:
    assert console_safe_text("Doing well \U0001f60e", "cp1252") == "Doing well"
    assert console_safe_text("caf\u00e9", "cp1252") == "caf\u00e9"


def test_spoken_output_removes_emoji_but_keeps_human_punctuation() -> None:
    assert speech_safe_text("Doing well \U0001f60e \u2014 just listening.") == (
        "Doing well - just listening."
    )
    assert speech_safe_text("What\u2019s up? \U0001f60e") == "What's up?"


def test_spoken_output_removes_markdown_but_keeps_its_content() -> None:
    response = """## Health status\n\n- **Drive C:** is almost full\n- [Check storage](https://example.com)\n"""

    assert speech_safe_text(response) == "Health status Drive C: is almost full Check storage"


def test_windows_stt_returns_only_confident_recognition() -> None:
    runner = FakeSpeechRunner(
        [
            json.dumps({"heard": True, "text": "open calculator", "confidence": 0.91}),
            json.dumps({"heard": True, "text": "unclear", "confidence": 0.1}),
        ]
    )
    recognizer = WindowsSpeechRecognizer(
        runner, minimum_confidence=0.35, capture_utterance=lambda _: b"\x00\x00" * 1600
    )

    heard = recognizer.listen(5)
    rejected = recognizer.listen(5)

    assert heard is not None and heard.text == "open calculator"
    assert rejected is None
    first_path = runner.calls[0]["payload"]["audio_path"]
    assert first_path.endswith(".wav")
    assert not Path(first_path).exists()


def test_wake_word_requires_confidence_threshold() -> None:
    runner = FakeSpeechRunner(
        [
            json.dumps({"heard": True, "text": "hey wyzer", "confidence": 0.8}),
            json.dumps({"heard": True, "text": "hey wyzer", "confidence": 0.2}),
        ]
    )
    detector = WindowsWakeWordDetector("hey wyzer", runner, minimum_confidence=0.55)

    assert detector.wait(3) is True
    assert detector.wait(3) is False
    assert runner.calls[0]["payload"]["wake_phrase"] == "hey wyzer"


def test_tts_passes_text_and_bounded_voice_settings() -> None:
    runner = FakeSpeechRunner([""])
    speaker = WindowsSpeechSynthesizer(runner, voice="Test Voice", rate=50, volume=200)

    speaker.speak("Hello from Wyzer")

    payload = runner.calls[0]["payload"]
    assert payload == {
        "text": "Hello from Wyzer",
        "voice": "Test Voice",
        "rate": 10,
        "volume": 100,
    }


def test_windows_phrase_detector_uses_small_control_grammar() -> None:
    runner = FakeSpeechRunner(
        [json.dumps({"heard": True, "text": "wyzer stop", "confidence": 0.92})]
    )
    detector = WindowsPhraseDetector(
        ["wyzer stop", "wyzer cancel"], runner, minimum_confidence=0.45
    )

    assert detector.wait(2) is True
    assert runner.calls[0]["payload"]["phrases"] == ["wyzer stop", "wyzer cancel"]
    assert runner.calls[0]["payload"]["timeout_seconds"] == 2


def test_windows_phrase_detector_returns_matched_control_phrase() -> None:
    runner = FakeSpeechRunner(
        [json.dumps({"heard": True, "text": "hey wyzer pause", "confidence": 0.91})]
    )
    detector = WindowsPhraseDetector(["hey wyzer stop", "hey wyzer pause"], runner)

    recognized = detector.recognize(2)

    assert recognized is not None
    assert recognized.text == "hey wyzer pause"


def test_windows_phrase_detector_cancelled_before_wait_does_not_start_runner() -> None:
    runner = FakeSpeechRunner([])
    detector = WindowsPhraseDetector(["wyzer stop"], runner)

    detector.cancel()

    assert detector.wait(2) is False
    assert runner.calls == []


def test_speech_diagnostic_reports_installed_recognizers_and_voices() -> None:
    runner = FakeSpeechRunner(
        [json.dumps({"recognizers": ["en-US: Desktop"], "voices": ["Microsoft David"]})]
    )

    diagnostic = WindowsSpeechRecognizer(runner).diagnose()

    assert diagnostic.available is True
    assert diagnostic.recognizers == ("en-US: Desktop",)
    assert diagnostic.voices == ("Microsoft David",)


def test_openwakeword_uses_supplied_onnx_model_and_microphone_frames(tmp_path: Path) -> None:
    model_path = tmp_path / "hey_Wyzer.onnx"
    model_path.write_bytes(b"model")

    class FakeModel:
        reset_count = 0

        def reset(self) -> None:
            self.reset_count += 1

        def predict(self, audio: Any) -> dict[str, float]:
            assert len(audio) == 1280
            return {"hey_Wyzer": 0.9}

    class FakeAudio:
        def __enter__(self) -> "FakeAudio":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, frames: int) -> tuple[bytes, bool]:
            return np.zeros(frames, dtype=np.int16).tobytes(), False

    model = FakeModel()
    detector = OpenWakeWordDetector(
        model_path, threshold=0.55, model=model, audio_factory=FakeAudio
    )

    assert detector.wait(1) is True
    assert model.reset_count == 1


def test_openwakeword_reuses_idle_microphone_stream_until_detection(tmp_path: Path) -> None:
    model_path = tmp_path / "hey_Wyzer.onnx"
    model_path.write_bytes(b"model")

    class FakeModel:
        reset_count = 0
        predictions = iter((0.0, 0.9))

        def reset(self) -> None:
            self.reset_count += 1

        def predict(self, audio: Any) -> dict[str, float]:
            del audio
            return {"hey_Wyzer": next(self.predictions)}

    class FakeAudio:
        enter_count = 0
        exit_count = 0
        read_count = 0

        def __enter__(self) -> "FakeAudio":
            self.enter_count += 1
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self.exit_count += 1

        def read(self, frames: int) -> tuple[bytes, bool]:
            self.read_count += 1
            if self.read_count == 1:
                time.sleep(0.005)
            return np.zeros(frames, dtype=np.int16).tobytes(), False

    model = FakeModel()
    audio = FakeAudio()
    detector = OpenWakeWordDetector(
        model_path, threshold=0.55, model=model, audio_factory=lambda: audio
    )

    assert detector.wait(0.001) is False
    assert audio.enter_count == 1
    assert audio.exit_count == 0

    assert detector.wait(1) is True
    assert audio.enter_count == 1
    assert audio.exit_count == 1
    assert model.reset_count == 2

    detector.close()
    assert audio.exit_count == 1


def test_find_wake_model_prefers_named_wyzer_model(tmp_path: Path) -> None:
    (tmp_path / "hey_wiser.onnx").write_bytes(b"one")
    expected = tmp_path / "hey_Wyzer.onnx"
    expected.write_bytes(b"two")

    assert find_wake_model(tmp_path).name.casefold() == expected.name.casefold()
    assert find_wake_model(tmp_path, "hey_wiser.onnx").name == "hey_wiser.onnx"


def test_faster_whisper_transcribes_local_capture_and_removes_wav(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class Segment:
        text = " open calculator "
        avg_logprob = -0.1
        no_speech_prob = 0.05

    class Model:
        def transcribe(self, audio: str, **kwargs: Any) -> tuple[list[Segment], object]:
            calls.append({"audio": audio, **kwargs})
            assert Path(audio).is_file()
            return [Segment()], object()

    def factory(*args: Any, **kwargs: Any) -> Model:
        calls.append({"model_args": args, "model_kwargs": kwargs})
        return Model()

    recognizer = FasterWhisperRecognizer(
        "small.en",
        device="cpu",
        download_root=tmp_path / "models",
        capture_utterance=lambda _: b"\x00\x00" * 1600,
        model_factory=factory,
    )

    result = recognizer.listen(5)

    assert result is not None and result.text == "open calculator"
    audio_path = Path(str(calls[1]["audio"]))
    assert not audio_path.exists()
    assert calls[0]["model_kwargs"]["device"] == "cpu"
    assert "Rocket League" in calls[1]["hotwords"]


def test_faster_whisper_diagnostic_accepts_explicit_cuda(tmp_path: Path) -> None:
    class Model:
        def transcribe(self, audio: str, **kwargs: Any) -> tuple[list[Any], object]:
            del audio, kwargs
            return [], object()

    recognizer = FasterWhisperRecognizer(
        "small.en",
        device="cuda",
        download_root=tmp_path,
        capture_utterance=lambda _: None,
        model_factory=lambda *args, **kwargs: Model(),
    )

    diagnostic = recognizer.diagnose()

    assert diagnostic.available is True
    assert "cuda" in diagnostic.message


def test_faster_whisper_rejects_no_speech_hallucination(tmp_path: Path) -> None:
    class Segment:
        text = "You"
        avg_logprob = -0.01
        no_speech_prob = 0.95

    class Model:
        def transcribe(self, audio: str, **kwargs: Any) -> tuple[list[Segment], object]:
            del audio, kwargs
            return [Segment()], object()

    recognizer = FasterWhisperRecognizer(
        "small.en",
        device="cpu",
        download_root=tmp_path,
        capture_utterance=lambda _: b"\x00\x00" * 1600,
        model_factory=lambda *args, **kwargs: Model(),
    )

    assert recognizer.listen(1) is None
