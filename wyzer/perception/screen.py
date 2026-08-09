"""Windows screenshot capture with no model-visible coordinates."""

from __future__ import annotations

import io
import platform
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class ScreenFrame:
    image_bytes: bytes
    left: int
    top: int
    right: int
    bottom: int
    source_width: int
    source_height: int
    sent_width: int
    sent_height: int
    scope: Literal["active_window", "full_desktop"]
    window_title: str | None = None

    def point_from_normalized(self, x: int, y: int) -> tuple[int, int]:
        """Convert 0..1000 vision coordinates into real desktop coordinates."""
        clamped_x = min(1000, max(0, x))
        clamped_y = min(1000, max(0, y))
        width = max(1, self.right - self.left)
        height = max(1, self.bottom - self.top)
        screen_x = self.left + round(width * clamped_x / 1000)
        screen_y = self.top + round(height * clamped_y / 1000)
        return screen_x, screen_y


class ScreenCapture(Protocol):
    available: bool
    unavailable_reason: str | None

    def capture(
        self,
        scope: Literal["active_window", "full_desktop"],
        *,
        max_dimension: int,
    ) -> ScreenFrame: ...


class PillowScreenCapture:
    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason: str | None = None
        self._ImageGrab = None
        self._Image = None
        if platform.system() != "Windows":
            self.unavailable_reason = "screen perception is available only on Windows"
            return
        try:
            from PIL import Image, ImageGrab
        except Exception as error:
            self.unavailable_reason = f"Pillow screen capture is unavailable: {error}"
            return
        self._Image = Image
        self._ImageGrab = ImageGrab
        self.available = True

    def capture(
        self,
        scope: Literal["active_window", "full_desktop"],
        *,
        max_dimension: int,
    ) -> ScreenFrame:
        if not self.available or self._ImageGrab is None or self._Image is None:
            raise RuntimeError(self.unavailable_reason or "screen capture is unavailable")

        left, top, right, bottom, title = self._capture_bounds(scope)
        if scope == "active_window":
            image = self._ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        else:
            image = self._ImageGrab.grab(all_screens=True)

        source_width, source_height = image.size
        if source_width <= 0 or source_height <= 0:
            raise RuntimeError("captured screenshot was empty")

        sent = image.convert("RGB")
        longest = max(sent.size)
        if longest > max_dimension:
            scale = max_dimension / longest
            target = (
                max(1, round(sent.width * scale)),
                max(1, round(sent.height * scale)),
            )
            sent = sent.resize(target, self._Image.Resampling.LANCZOS)

        output = io.BytesIO()
        sent.save(output, format="JPEG", quality=88, optimize=True)
        return ScreenFrame(
            image_bytes=output.getvalue(),
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            source_width=source_width,
            source_height=source_height,
            sent_width=sent.width,
            sent_height=sent.height,
            scope=scope,
            window_title=title,
        )

    @staticmethod
    def _capture_bounds(
        scope: Literal["active_window", "full_desktop"],
    ) -> tuple[int, int, int, int, str | None]:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        if scope == "active_window":
            hwnd = int(user32.GetForegroundWindow())
            if not hwnd:
                raise RuntimeError("no foreground window is available")
            rect = ctypes.wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                raise RuntimeError("could not read the foreground window bounds")
            left, top, right, bottom = (
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            )
            if right <= left or bottom <= top:
                raise RuntimeError("foreground window has invalid bounds")
            length = int(user32.GetWindowTextLengthW(hwnd))
            buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            return left, top, right, bottom, buffer.value or None

        # Virtual-screen metrics include every monitor and preserve negative coordinates.
        left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            raise RuntimeError("could not read virtual desktop bounds")
        return left, top, left + width, top + height, None
