import asyncio
from pathlib import Path

import pytest

from tests.fake_windows import FakeWindowsBackend
from wyzer.app.orchestrator import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.memory import MemoryStore, SensitiveMemoryError
from wyzer.tools import create_default_registry
from wyzer.workers import InProcessExecutor


def assistant_with_memory(path: Path) -> Orchestrator:
    registry = create_default_registry(FakeWindowsBackend())
    return Orchestrator(
        registry,
        InProcessExecutor(registry),
        FakeChatProvider(available=False),
        memory=MemoryStore(path),
    )


def ask(assistant: Orchestrator, text: str) -> str:
    return asyncio.run(assistant.handle(text)).text


def test_explicit_memory_persists_between_assistant_sessions(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    first = assistant_with_memory(database)
    assert "Koly" in ask(first, "remember that my name is Koly")

    second = assistant_with_memory(database)

    assert second.conversation.snapshot().remembered_facts == ["my name is Koly"]
    assert "my name is Koly" in ask(second, "what do you remember about me")


def test_memory_can_be_selectively_forgotten(tmp_path: Path) -> None:
    assistant = assistant_with_memory(tmp_path / "memory.db")
    ask(assistant, "remember that Spotify belongs on monitor two")

    response = ask(assistant, "forget Spotify")

    assert "forgot 1" in response
    assert assistant.conversation.snapshot().remembered_facts == []


def test_sensitive_memory_is_rejected(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")

    with pytest.raises(SensitiveMemoryError):
        store.remember("my password is swordfish")

    assert store.list() == []
