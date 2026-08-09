import asyncio

from tests.fakes import text_response
from wyzer.app.cli import _voice_interrupt_phrases, normalize_spoken_command
from wyzer.app.orchestrator import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.tools import ToolRegistry
from wyzer.workers import InProcessExecutor


def test_spoken_past_tense_is_repaired_before_orchestration() -> None:
    assert normalize_spoken_command("Opened Chrome") == "open Chrome"


def test_casual_voice_request_goes_to_model_first() -> None:
    provider = FakeChatProvider([text_response("Sure — let's talk.")])
    registry = ToolRegistry()
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    response = asyncio.run(assistant.handle("what's on your mind"))

    assert response.text == "Sure — let's talk."
    assert len(provider.requests) == 1


def test_voice_stop_remains_a_local_control_command() -> None:
    provider = FakeChatProvider([text_response("unused")])
    registry = ToolRegistry()
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)
    response = asyncio.run(assistant.handle("cancel"))
    assert response.text == "There is no active task."
    assert provider.requests == []


def test_voice_interrupt_phrases_allow_bare_stop_during_active_task() -> None:
    phrases = _voice_interrupt_phrases("hey wyzer", allow_bare=True)

    assert "stop" in phrases
    assert "cancel" in phrases
    assert "wyzer stop" in phrases
    assert "hey wyzer stop" in phrases
    assert "pause" in phrases
    assert "hey wyzer pause" in phrases
    assert "hey wyzer" not in phrases


def test_voice_interrupt_phrases_require_wyzer_while_tts_is_playing() -> None:
    phrases = _voice_interrupt_phrases("hey wyzer", allow_bare=False)

    assert "stop" not in phrases
    assert "cancel" not in phrases
    assert "wyzer stop" in phrases
    assert "hey wyzer" in phrases
