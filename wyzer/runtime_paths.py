"""Stable per-user paths for development and installed Wyzer copies."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from wyzer.config import WyzerSettings


def data_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return Wyzer's writable data directory.

    Development keeps the historical ``.wyzer`` directory. The installer sets
    WYZER_HOME to a stable directory below LocalAppData.
    """
    env = os.environ if environ is None else environ
    configured = env.get("WYZER_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else (Path.cwd() / ".wyzer")


def find_config_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    configured = env.get("WYZER_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = Path.cwd() / "wyzer.toml"
    if local.is_file():
        return local
    installed = data_home(env) / "wyzer.toml"
    return installed if installed.is_file() else None


def configure_runtime_paths(
    settings: WyzerSettings,
    config_path: Path | None,
    environ: Mapping[str, str] | None = None,
) -> WyzerSettings:
    """Resolve all mutable/model paths independently of the launch directory."""
    home = data_home(environ)
    base = config_path.parent.resolve() if config_path is not None else Path.cwd()

    def resolve(path: Path) -> Path:
        if path.is_absolute():
            return path
        parts = path.parts
        if parts and parts[0] == ".wyzer":
            return (home.joinpath(*parts[1:])).resolve()
        return (base / path).resolve()

    return settings.model_copy(
        update={
            "memory": settings.memory.model_copy(
                update={"database_path": resolve(settings.memory.database_path)}
            ),
            "task_engine": settings.task_engine.model_copy(
                update={"state_path": resolve(settings.task_engine.state_path)}
            ),
            "speech": settings.speech.model_copy(
                update={
                    "wake_model_directory": resolve(settings.speech.wake_model_directory),
                    "whisper_download_root": resolve(settings.speech.whisper_download_root),
                }
            ),
        }
    )
