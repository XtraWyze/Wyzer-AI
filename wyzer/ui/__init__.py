"""Optional PySide6 desktop companion UI for Wyzer."""

from __future__ import annotations

from typing import Any

from wyzer.app.orchestrator import Orchestrator
from wyzer.brain import ChatProvider
from wyzer.config import WyzerSettings

__all__ = ["run_desktop_ui"]


def run_desktop_ui(
    assistant: Orchestrator,
    provider: ChatProvider,
    settings: WyzerSettings,
    *,
    app: Any | None = None,
    voice_enabled: bool = False,
    wake_phrase: str | None = None,
) -> int:
    """Import the optional Qt UI lazily so normal CLI installs do not require PySide6."""
    from wyzer.ui.desktop import run_desktop_ui as _run

    return _run(
        assistant,
        provider,
        settings,
        app=app,
        voice_enabled=voice_enabled,
        wake_phrase=wake_phrase,
    )
