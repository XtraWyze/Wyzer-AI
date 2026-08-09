"""Windows Core Audio integration and deterministic audio-session resolution."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar, Protocol

from wyzer.tools.base import ToolExecutionError


@dataclass(frozen=True, slots=True)
class MasterAudioState:
    level: int
    muted: bool


@dataclass(frozen=True, slots=True)
class AudioSession:
    session_id: str
    display_name: str
    process_name: str | None
    process_id: int | None
    level: int
    muted: bool
    active: bool | None

    def model_data(self, duplicate_count: int = 1) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.display_name,
            "process": self.process_name,
            "process_id": self.process_id,
            "volume": self.level,
            "muted": self.muted,
            "active": self.active,
            "multiple_sessions": duplicate_count > 1,
        }


class CoreAudioAdapter(Protocol):
    def read_master(self) -> MasterAudioState: ...

    def write_master(
        self, level: int | None = None, muted: bool | None = None
    ) -> MasterAudioState: ...

    def list_sessions(self) -> list[AudioSession]: ...

    def write_session(
        self, session_id: str, level: int | None = None, muted: bool | None = None
    ) -> AudioSession: ...

    def diagnostic(self) -> dict[str, Any]: ...


class AudioMixer:
    """Applies validated audio operations without leaking COM details to tools."""

    _ALIASES: ClassVar[dict[str, str]] = {
        "chrome": "chrome",
        "edge": "msedge",
        "spotify": "spotify",
        "discord": "discord",
        "firefox": "firefox",
        "vlc": "vlc",
    }

    def __init__(
        self,
        adapter: CoreAudioAdapter,
        *,
        master_step: int = 10,
        application_step: int = 10,
        match_threshold: float = 0.72,
        ambiguity_margin: float = 0.08,
        control_all_matching_sessions: bool = True,
        core_audio_timeout_seconds: float = 5,
        fallback: Callable[[str], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._master_step = master_step
        self._application_step = application_step
        self._match_threshold = match_threshold
        self._ambiguity_margin = ambiguity_margin
        self._control_all_matching_sessions = control_all_matching_sessions
        self._core_audio_timeout_seconds = core_audio_timeout_seconds
        self._fallback = fallback

    def control_master(
        self, operation: str, amount: int | None = None, level: int | None = None
    ) -> dict[str, Any]:
        try:
            before = self._adapter.read_master()
            after = self._apply_master(before, operation, amount, level)
            return {
                "target": "master",
                "operation": operation,
                "previous_level": before.level,
                "new_level": after.level,
                "muted": after.muted,
                "fallback_used": False,
            }
        except ToolExecutionError:
            if self._fallback is None or operation not in {"increase", "decrease", "toggle_mute"}:
                raise
            legacy = {"increase": "up", "decrease": "down", "toggle_mute": "mute_toggle"}
            self._fallback(legacy[operation])
            return {
                "target": "master",
                "operation": operation,
                "previous_level": None,
                "new_level": None,
                "muted": None,
                "fallback_used": True,
                "warnings": [
                    "Windows accepted a media-key fallback; the exact resulting level is "
                    "unavailable."
                ],
            }
        except Exception as error:
            raise ToolExecutionError(
                "AUDIO_CONTROL_UNAVAILABLE",
                "I couldn't access Windows audio controls.",
                details={"exception_type": error.__class__.__name__},
            ) from error

    def list_sessions(self) -> dict[str, Any]:
        try:
            sessions = self._adapter.list_sessions()
        except ToolExecutionError:
            raise
        except Exception as error:
            raise ToolExecutionError(
                "AUDIO_SESSION_ENUMERATION_FAILED",
                "I couldn't list Windows audio sessions.",
                details={"exception_type": error.__class__.__name__},
            ) from error
        process_counts: dict[str, int] = {}
        for session in sessions:
            key = _normalized(session.process_name or session.display_name)
            process_counts[key] = process_counts.get(key, 0) + 1
        return {
            "sessions": [
                session.model_data(
                    process_counts[_normalized(session.process_name or session.display_name)]
                )
                for session in sessions
            ],
            "count": len(sessions),
        }

    def control_application(
        self,
        application: str,
        operation: str,
        amount: int | None = None,
        level: int | None = None,
        scope: str = "all",
    ) -> dict[str, Any]:
        sessions = self._adapter.list_sessions()
        matched, candidates = self._resolve(sessions, application)
        if not matched:
            if candidates:
                raise ToolExecutionError(
                    "AMBIGUOUS_AUDIO_SESSION",
                    "More than one audio application matched.",
                    details={"application": application, "candidates": candidates},
                )
            raise ToolExecutionError(
                "AUDIO_SESSION_NOT_FOUND",
                f"{application} does not currently have an active audio session.",
                details={"application": application},
            )
        selected = (
            matched if scope == "all" and self._control_all_matching_sessions else matched[:1]
        )
        updated: list[AudioSession] = []
        for session in selected:
            try:
                updated.append(self._apply_session(session, operation, amount, level))
            except ToolExecutionError:
                raise
            except Exception as error:
                raise ToolExecutionError(
                    "AUDIO_SESSION_DISAPPEARED",
                    f"{session.display_name}'s audio session disappeared before it could be "
                    "changed.",
                    details={
                        "session_id": session.session_id,
                        "exception_type": error.__class__.__name__,
                    },
                ) from error
        resulting_levels = sorted({session.level for session in updated})
        return {
            "target": matched[0].display_name,
            "matched_process": matched[0].process_name,
            "operation": operation,
            "requested_level": level,
            "sessions_matched": len(matched),
            "sessions_changed": len(updated),
            "resulting_levels": resulting_levels,
            "muted": all(session.muted for session in updated),
            "session_ids": [session.session_id for session in updated],
        }

    def mute_all_except(self, applications: list[str]) -> dict[str, Any]:
        sessions = self._adapter.list_sessions()
        keep_ids: set[str] = set()
        for application in applications:
            matched, candidates = self._resolve(sessions, application)
            if not matched:
                if candidates:
                    raise ToolExecutionError(
                        "AMBIGUOUS_AUDIO_SESSION",
                        "More than one audio application matched.",
                        details={"application": application, "candidates": candidates},
                    )
                raise ToolExecutionError(
                    "AUDIO_SESSION_NOT_FOUND",
                    f"{application} does not currently have an active audio session.",
                    details={"application": application},
                )
            keep_ids.update(session.session_id for session in matched)
        changed = 0
        for session in sessions:
            if session.session_id not in keep_ids and not session.muted:
                self._adapter.write_session(session.session_id, muted=True)
                changed += 1
        return {
            "operation": "mute_all_except",
            "kept_applications": applications,
            "sessions_changed": changed,
            "sessions_excluded": len(keep_ids),
        }

    def diagnostic(self) -> dict[str, Any]:
        state = self._adapter.read_master()
        return {
            "master": {"level": state.level, "muted": state.muted},
            "sessions": self.list_sessions()["sessions"],
            **self._adapter.diagnostic(),
        }

    def _apply_master(
        self, before: MasterAudioState, operation: str, amount: int | None, level: int | None
    ) -> MasterAudioState:
        if operation == "get":
            return before
        if operation == "mute":
            return self._adapter.write_master(muted=True)
        if operation == "unmute":
            return self._adapter.write_master(muted=False)
        if operation == "toggle_mute":
            return self._adapter.write_master(muted=not before.muted)
        if operation == "set":
            assert level is not None
            return self._adapter.write_master(level=level)
        step = amount if amount is not None else self._master_step
        target = before.level + step if operation == "increase" else before.level - step
        return self._adapter.write_master(level=max(0, min(100, target)))

    def _apply_session(
        self, session: AudioSession, operation: str, amount: int | None, level: int | None
    ) -> AudioSession:
        if operation == "get":
            return session
        if operation == "mute":
            return self._adapter.write_session(session.session_id, muted=True)
        if operation == "unmute":
            return self._adapter.write_session(session.session_id, muted=False)
        if operation == "toggle_mute":
            return self._adapter.write_session(session.session_id, muted=not session.muted)
        if operation == "set":
            assert level is not None
            return self._adapter.write_session(session.session_id, level=level)
        step = amount if amount is not None else self._application_step
        target = session.level + step if operation == "increase" else session.level - step
        return self._adapter.write_session(session.session_id, level=max(0, min(100, target)))

    def _resolve(
        self, sessions: list[AudioSession], application: str
    ) -> tuple[list[AudioSession], list[dict[str, str | None]]]:
        query = _normalized(application)
        exact_id = [session for session in sessions if session.session_id == application]
        if exact_id:
            return exact_id, []
        alias = self._ALIASES.get(query, query)
        executable = [
            session for session in sessions if _normalized(session.process_name or "") == alias
        ]
        if executable:
            return executable, []
        named = [session for session in sessions if _normalized(session.display_name) == query]
        if named:
            return named, []
        scored = sorted(
            ((_similarity(query, _session_name(session)), session) for session in sessions),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < self._match_threshold:
            return [], []
        top_score = scored[0][0]
        names = {
            _session_name(session)
            for score, session in scored
            if score >= top_score - self._ambiguity_margin
        }
        if len(names) > 1:
            return [], [
                {"name": session.display_name, "process": session.process_name}
                for score, session in scored
                if score >= top_score - self._ambiguity_margin
            ][:5]
        return [session for score, session in scored if _session_name(session) in names], []


class PycawCoreAudio:
    """Lazy pycaw adapter that initializes COM in every calling worker thread."""

    def __init__(self, cache_directory: Path | None = None) -> None:
        self._cache_directory = (
            cache_directory or Path(tempfile.gettempdir()) / "wyzer-comtypes-gen"
        )

    def read_master(self) -> MasterAudioState:
        with self._com() as api:
            endpoint = api["endpoint"]()
            return MasterAudioState(
                _percent(endpoint.GetMasterVolumeLevelScalar()), bool(endpoint.GetMute())
            )

    def write_master(self, level: int | None = None, muted: bool | None = None) -> MasterAudioState:
        with self._com() as api:
            endpoint = api["endpoint"]()
            if level is not None:
                endpoint.SetMasterVolumeLevelScalar(level / 100, None)
            if muted is not None:
                endpoint.SetMute(muted, None)
            return MasterAudioState(
                _percent(endpoint.GetMasterVolumeLevelScalar()), bool(endpoint.GetMute())
            )

    def list_sessions(self) -> list[AudioSession]:
        with self._com() as api:
            return [
                _session_from_pycaw(session, api["ISimpleAudioVolume"])
                for session in api["sessions"]()
            ]

    def write_session(
        self, session_id: str, level: int | None = None, muted: bool | None = None
    ) -> AudioSession:
        with self._com() as api:
            for session in api["sessions"]():
                candidate = _session_from_pycaw(session, api["ISimpleAudioVolume"])
                if candidate.session_id != session_id:
                    continue
                volume = session._ctl.QueryInterface(api["ISimpleAudioVolume"])
                if level is not None:
                    volume.SetMasterVolume(level / 100, None)
                if muted is not None:
                    volume.SetMute(muted, None)
                return _session_from_pycaw(session, api["ISimpleAudioVolume"])
        raise ToolExecutionError(
            "AUDIO_SESSION_DISAPPEARED", "The audio session disappeared before it could be changed."
        )

    def diagnostic(self) -> dict[str, Any]:
        with self._com() as api:
            device = api["device"]()
            return {"output_device": str(getattr(device, "FriendlyName", "Default output device"))}

    @contextmanager
    def _com(self) -> Iterator[dict[str, Any]]:
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        os.environ["COMTYPES_GEN_DIR"] = str(self._cache_directory)
        try:
            from ctypes import POINTER, cast

            comtypes = import_module("comtypes")
            comtypes_client = import_module("comtypes.client")
            pycaw = import_module("pycaw.pycaw")
        except ImportError as error:
            raise ToolExecutionError(
                "CORE_AUDIO_UNAVAILABLE",
                "Windows Core Audio support is unavailable. Install the optional pycaw dependency.",
            ) from error
        comtypes_client.__dict__["gen_dir"] = str(self._cache_directory)
        initialized_here = False
        try:
            comtypes.CoInitialize()
            initialized_here = True
        except OSError as error:
            # pywinauto may already have initialized this worker thread in the
            # opposite COM apartment. RPC_E_CHANGED_MODE means COM is usable,
            # but its apartment cannot be changed; continue in that apartment.
            hresult = getattr(error, "winerror", getattr(error, "hresult", None))
            if hresult != -2147417850:  # RPC_E_CHANGED_MODE
                raise
        try:
            device = pycaw.AudioUtilities.GetSpeakers()
            if device is None:
                raise ToolExecutionError(
                    "AUDIO_DEVICE_UNAVAILABLE", "No default output device is available."
                )

            def endpoint() -> Any:
                # Modern pycaw returns an AudioDevice wrapper. Its public
                # EndpointVolume property performs the underlying IMMDevice
                # activation for us. Older pycaw releases returned the raw COM
                # device, so retain a compatibility fallback for those versions.
                endpoint_volume = getattr(device, "EndpointVolume", None)
                if endpoint_volume is not None:
                    return endpoint_volume

                raw_device = getattr(device, "_dev", device)
                activate = getattr(raw_device, "Activate", None)
                if activate is None:
                    raise ToolExecutionError(
                        "AUDIO_ENDPOINT_UNAVAILABLE",
                        "The default output device does not expose endpoint-volume control.",
                    )
                interface = activate(pycaw.IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
                return cast(interface, POINTER(pycaw.IAudioEndpointVolume))

            yield {
                "endpoint": endpoint,
                "sessions": pycaw.AudioUtilities.GetAllSessions,
                "device": lambda: device,
                "ISimpleAudioVolume": pycaw.ISimpleAudioVolume,
            }
        except ToolExecutionError:
            raise
        except Exception as error:
            raise ToolExecutionError(
                "CORE_AUDIO_FAILED",
                "Windows Core Audio did not complete the request.",
                details={"exception_type": error.__class__.__name__},
            ) from error
        finally:
            if initialized_here:
                comtypes.CoUninitialize()


def _session_from_pycaw(session: Any, interface: Any) -> AudioSession:
    process = getattr(session, "Process", None)
    try:
        process_name = process.name() if process is not None else None
    except Exception:
        process_name = None
    try:
        process_id = int(process.pid) if process is not None else None
    except Exception:
        process_id = None
    display = str(getattr(session, "DisplayName", "") or process_name or "System sounds")
    identifier = str(
        getattr(session, "SessionIdentifier", "") or getattr(session, "InstanceIdentifier", "")
    )
    if not identifier:
        identifier = f"{process_id or 'system'}:{display}"
    volume = session._ctl.QueryInterface(interface)
    active = getattr(session, "State", None)
    return AudioSession(
        session_id=identifier,
        display_name=display,
        process_name=process_name,
        process_id=process_id,
        level=_percent(volume.GetMasterVolume()),
        muted=bool(volume.GetMute()),
        active=None if active is None else str(active).casefold() != "inactive",
    )


def _percent(value: float) -> int:
    return max(0, min(100, round(float(value) * 100)))


def _normalized(value: str) -> str:
    compact = "".join(re.findall(r"[a-z0-9]+", value.casefold()))
    return compact.removesuffix("exe")


def _session_name(session: AudioSession) -> str:
    return _normalized(session.process_name or session.display_name)


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    from difflib import SequenceMatcher

    return SequenceMatcher(None, left, right).ratio()
