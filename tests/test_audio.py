from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from tests.fake_windows import FakeWindowsBackend
from tests.fakes import text_response, tool_response
from wyzer.app import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.desktop import audio as audio_module
from wyzer.desktop.audio import (
    AudioMixer,
    AudioSession,
    CoreAudioAdapter,
    MasterAudioState,
    PycawCoreAudio,
)
from wyzer.tools import ToolExecutionError, create_default_registry
from wyzer.workers import InProcessExecutor


class FakeCoreAudio(CoreAudioAdapter):
    def __init__(self) -> None:
        self.master = MasterAudioState(level=65, muted=False)
        self.sessions = [
            AudioSession("chrome:1", "Google Chrome", "chrome.exe", 10, 60, False, True),
            AudioSession("chrome:2", "Google Chrome", "chrome.exe", 11, 40, False, True),
            AudioSession("spotify:1", "Spotify", "spotify.exe", 20, 70, False, True),
            AudioSession("system", "System sounds", None, None, 30, False, None),
        ]
        self.initialized = 0
        self.cleaned_up = 0
        self.disappearing: set[str] = set()

    def read_master(self) -> MasterAudioState:
        self.initialized += 1
        self.cleaned_up += 1
        return self.master

    def write_master(self, level: int | None = None, muted: bool | None = None) -> MasterAudioState:
        self.initialized += 1
        self.cleaned_up += 1
        self.master = MasterAudioState(
            self.master.level if level is None else level,
            self.master.muted if muted is None else muted,
        )
        return self.master

    def list_sessions(self) -> list[AudioSession]:
        self.initialized += 1
        self.cleaned_up += 1
        return list(self.sessions)

    def write_session(
        self, session_id: str, level: int | None = None, muted: bool | None = None
    ) -> AudioSession:
        self.initialized += 1
        self.cleaned_up += 1
        if session_id in self.disappearing:
            raise RuntimeError("gone")
        for index, session in enumerate(self.sessions):
            if session.session_id == session_id:
                updated = replace(
                    session,
                    level=session.level if level is None else level,
                    muted=session.muted if muted is None else muted,
                )
                self.sessions[index] = updated
                return updated
        raise RuntimeError("gone")

    def diagnostic(self) -> dict[str, str]:
        return {"output_device": "Fake Speakers"}


def mixer(
    core: FakeCoreAudio | None = None,
    *,
    fallback: Callable[[str], None] | None = None,
) -> tuple[AudioMixer, FakeCoreAudio]:
    audio = core or FakeCoreAudio()
    return AudioMixer(audio, fallback=fallback), audio


def test_master_audio_get_relative_set_clamp_and_mute_operations() -> None:
    service, _ = mixer()
    assert service.control_master("get")["new_level"] == 65
    assert service.control_master("decrease")["new_level"] == 55
    assert service.control_master("decrease", amount=25)["new_level"] == 30
    assert service.control_master("set", level=40)["new_level"] == 40
    assert service.control_master("increase", amount=100)["new_level"] == 100
    assert service.control_master("decrease", amount=100)["new_level"] == 0
    assert service.control_master("mute")["muted"] is True
    assert service.control_master("unmute")["muted"] is False
    assert service.control_master("toggle_mute")["muted"] is True


def test_master_fallback_never_claims_exact_level() -> None:
    class BrokenCore(FakeCoreAudio):
        def read_master(self) -> MasterAudioState:
            raise ToolExecutionError("CORE_AUDIO_UNAVAILABLE", "unavailable")

    presses: list[str] = []
    service, _ = mixer(BrokenCore(), fallback=presses.append)
    result = service.control_master("decrease")
    assert result["fallback_used"] is True
    assert result["new_level"] is None
    assert presses == ["down"]
    with pytest.raises(ToolExecutionError, match="unavailable"):
        service.control_master("set", level=40)


def test_pycaw_uses_a_writable_comtypes_cache_before_optional_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "comtypes-cache"
    controller = PycawCoreAudio(cache)

    def unavailable(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(audio_module, "import_module", unavailable)
    with pytest.raises(ToolExecutionError) as error:
        controller.read_master()
    assert error.value.code == "CORE_AUDIO_UNAVAILABLE"
    assert cache.is_dir()
    assert os.environ["COMTYPES_GEN_DIR"] == str(cache)


def test_list_sessions_is_compact_and_handles_system_session() -> None:
    service, _ = mixer()
    listed = service.list_sessions()
    assert listed["count"] == 4
    assert listed["sessions"][0]["multiple_sessions"] is True
    assert listed["sessions"][-1]["process"] is None


def test_application_resolution_exact_executable_alias_and_fuzzy() -> None:
    service, _ = mixer()
    assert service.control_application("chrome:1", "get")["sessions_changed"] == 1
    assert service.control_application("chrome.exe", "set", level=30)["sessions_changed"] == 2
    assert service.control_application("Chrome", "decrease", amount=20)["resulting_levels"] == [10]
    assert service.control_application("Spotfy", "get")["target"] == "Spotify"


def test_application_multiple_sessions_scope_and_muting() -> None:
    service, _ = mixer()
    result = service.control_application("Chrome", "mute")
    assert result["sessions_matched"] == 2
    assert result["sessions_changed"] == 2
    assert result["muted"] is True
    one = service.control_application("Chrome", "unmute", scope="one")
    assert one["sessions_changed"] == 1


def test_ambiguous_and_missing_application_change_nothing() -> None:
    service, core = mixer()
    core.sessions.extend(
        [
            AudioSession("music-one", "Music One", "music_one.exe", 30, 20, False, True),
            AudioSession("music-two", "Music Two", "music_two.exe", 31, 20, False, True),
        ]
    )
    before = list(core.sessions)
    with pytest.raises(ToolExecutionError) as ambiguous:
        service.control_application("music", "mute")
    assert ambiguous.value.code == "AMBIGUOUS_AUDIO_SESSION"
    assert core.sessions == before
    with pytest.raises(ToolExecutionError) as missing:
        service.control_application("VLC", "mute")
    assert missing.value.code == "AUDIO_SESSION_NOT_FOUND"


def test_disappearing_session_is_structured_and_cleanup_pairs_initialization() -> None:
    service, core = mixer()
    core.disappearing.add("spotify:1")
    with pytest.raises(ToolExecutionError) as error:
        service.control_application("Spotify", "mute")
    assert error.value.code == "AUDIO_SESSION_DISAPPEARED"
    assert core.initialized == core.cleaned_up


def test_batch_mutes_everything_except_requested_application() -> None:
    service, core = mixer()
    result = service.mute_all_except(["Spotify"])
    assert result["operation"] == "mute_all_except"
    assert result["sessions_changed"] == 3
    assert all(session.muted for session in core.sessions if session.session_id != "spotify:1")
    assert (
        next(session for session in core.sessions if session.session_id == "spotify:1").muted
        is False
    )


def test_audio_tool_schemas_reject_contradictory_arguments() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    executor = InProcessExecutor(registry)

    result = asyncio.run(
        executor.execute(
            "control_master_audio", {"operation": "set", "amount": 20}, uuid4(), uuid4()
        )
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == "INVALID_TOOL_ARGUMENTS"
    assert "control_volume" not in set(registry)


def test_native_tool_loop_handles_master_application_list_and_compound_audio_requests() -> None:
    backend = FakeWindowsBackend()
    backend.audio_sessions.append(
        {
            "session_id": "spotify:1",
            "name": "Spotify",
            "process": "spotify.exe",
            "process_id": 44,
            "volume": 70,
            "muted": False,
            "active": True,
            "multiple_sessions": False,
        }
    )
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(("control_master_audio", {"operation": "decrease", "amount": 25})),
            text_response("I lowered the master volume by 25 points."),
            tool_response(
                ("control_application_audio", {"application": "Spotify", "operation": "mute"})
            ),
            text_response("Spotify is muted."),
            tool_response(("list_audio_sessions", {})),
            text_response("Chrome is the app playing audio."),
            tool_response(
                ("open_application", {"application": "Calculator"}),
                ("control_master_audio", {"operation": "decrease"}),
            ),
            text_response("Calculator is open and I turned the master volume down."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    assert "25" in asyncio.run(assistant.handle("Lower the volume by 25")).text
    assert "muted" in asyncio.run(assistant.handle("Mute Spotify")).text
    assert "playing" in asyncio.run(assistant.handle("List apps playing audio")).text
    response = asyncio.run(assistant.handle("Open Calculator and turn the volume down"))

    assert "Calculator" in response.text
    assert [result.tool for result in assistant.world.snapshot().recent_tool_calls[-2:]] == [
        "open_application",
        "control_master_audio",
    ]


def test_audio_followup_target_is_compact_context_not_full_session_dump() -> None:
    backend = FakeWindowsBackend()
    backend.audio_sessions.append(
        {
            "session_id": "spotify:1",
            "name": "Spotify",
            "process": "spotify.exe",
            "process_id": 44,
            "volume": 70,
            "muted": False,
            "active": True,
            "multiple_sessions": False,
        }
    )
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(
                (
                    "control_application_audio",
                    {"application": "Spotify", "operation": "set", "level": 40},
                )
            ),
            text_response("Spotify is at 40 percent."),
            tool_response(
                ("control_application_audio", {"application": "Spotify", "operation": "mute"})
            ),
            text_response("Spotify is muted."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    asyncio.run(assistant.handle("Set Spotify to 40"))
    assert "muted" in asyncio.run(assistant.handle("Mute it")).text

    system = provider.requests[2][0][0].content or ""
    assert '"recent_audio_targets"' in system
    assert "Spotify" in system
    assert "chrome:1" not in system


def test_audio_failure_does_not_prevent_later_requests() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    provider = FakeChatProvider(
        [
            tool_response(
                ("control_application_audio", {"application": "VLC", "operation": "mute"})
            ),
            text_response("VLC doesn't currently have an active audio session."),
            text_response("I'm still listening."),
        ]
    )
    assistant = Orchestrator(registry, InProcessExecutor(registry), provider)

    assert "doesn't" in asyncio.run(assistant.handle("Mute VLC")).text
    assert asyncio.run(assistant.handle("Are you still there?")).text == "I'm still listening."
