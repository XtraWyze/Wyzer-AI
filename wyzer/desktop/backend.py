"""Interface implemented by real and fake desktop backends."""

from __future__ import annotations

from typing import Protocol

from wyzer.models import DesktopPerception, WindowInfo


class DesktopBackend(Protocol):
    def get_foreground_window(self) -> WindowInfo | None: ...

    def list_open_windows(self) -> list[WindowInfo]: ...

    def perceive_focused_window(self) -> DesktopPerception: ...
