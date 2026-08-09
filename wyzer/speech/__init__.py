"""Optional local speech adapters."""

from wyzer.speech.kokoro import KokoroSpeechSynthesizer
from wyzer.speech.openwakeword import OpenWakeWordDetector, find_wake_model
from wyzer.speech.tts import SpeechSynthesizer, create_speech_synthesizer
from wyzer.speech.whisper import FasterWhisperRecognizer
from wyzer.speech.windows import (
    SpeechDiagnostic,
    SpeechRecognition,
    WindowsPhraseDetector,
    WindowsSpeechRecognizer,
    WindowsSpeechSynthesizer,
    WindowsWakeWordDetector,
)

__all__ = [
    "FasterWhisperRecognizer",
    "KokoroSpeechSynthesizer",
    "OpenWakeWordDetector",
    "SpeechDiagnostic",
    "SpeechRecognition",
    "SpeechSynthesizer",
    "WindowsPhraseDetector",
    "WindowsSpeechRecognizer",
    "WindowsSpeechSynthesizer",
    "WindowsWakeWordDetector",
    "create_speech_synthesizer",
    "find_wake_model",
]
