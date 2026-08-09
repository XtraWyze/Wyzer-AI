from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from wyzer.models import ConfirmationMode
from wyzer.perception.screen import ScreenFrame
from wyzer.tools.base import ToolContext, ToolExecutionError
from wyzer.tools.desktop_interaction import (
    DesktopActionResult,
    DesktopElement,
    DesktopInspectionResult,
)
from wyzer.tools.perception import (
    ActivateVisualTargetArguments,
    ActivateVisualTargetTool,
    InspectScreenArguments,
    InspectScreenTool,
)


class FakeCapture:
    available = True
    unavailable_reason = None

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def capture(self, scope: str, *, max_dimension: int) -> ScreenFrame:
        self.scopes.append(scope)
        assert max_dimension == 1600
        return ScreenFrame(
            image_bytes=b"fake-jpeg",
            left=100,
            top=200,
            right=1100,
            bottom=700,
            source_width=1000,
            source_height=500,
            sent_width=1000,
            sent_height=500,
            scope="active_window" if scope == "active_window" else "full_desktop",
            window_title="Setup",
        )


class FakeVision:
    available = True
    unavailable_reason = None
    model = "qwen3.5:test"

    def __init__(self, *, confidence: float = 0.92, found: bool = True) -> None:
        self.confidence = confidence
        self.found = found
        self.queries: list[str] = []

    def analyze(self, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        assert image_bytes == b"fake-jpeg"
        self.queries.append(prompt)
        return {
            "summary": "An installer is ready.",
            "visible_text": ["Ready to Install", "Install", "Cancel"],
            "relevant_elements": [{"label": "Install", "kind": "button", "state": "enabled"}],
            "warnings": [],
        }

    def locate(self, image_bytes: bytes, target: str) -> dict[str, Any]:
        assert image_bytes == b"fake-jpeg"
        self.queries.append(target)
        return {
            "found": self.found,
            "x": 750 if self.found else None,
            "y": 800 if self.found else None,
            "confidence": self.confidence,
            "description": "Install button near the lower-right.",
        }


class FakeUIA:
    available = True
    unavailable_reason = None

    def __init__(self) -> None:
        self.inspections: list[str | None] = []
        self.clicks: list[str] = []

    def inspect(self, query: str | None, limit: int) -> DesktopInspectionResult:
        self.inspections.append(query)
        assert limit in {20, 40}
        return DesktopInspectionResult(
            window_title="Setup",
            application="setup.exe",
            elements=[
                DesktopElement(
                    element_id="dui_fake_continue",
                    name="Continue",
                    control_type="Button",
                    enabled=True,
                    visible=True,
                )
            ],
        )

    def click(self, element_id: str) -> DesktopActionResult:
        self.clicks.append(element_id)
        return DesktopActionResult(action="click", target="Continue")

    def type_text(self, text: str) -> DesktopActionResult:
        raise NotImplementedError

    def press_key(self, key: str, presses: int) -> DesktopActionResult:
        raise NotImplementedError


class BrokenVision(FakeVision):
    def analyze(self, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        raise RuntimeError("vision unavailable")

    def locate(self, image_bytes: bytes, target: str) -> dict[str, Any]:
        raise RuntimeError("vision unavailable")


class FakePointer:
    available = True
    unavailable_reason = None

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


def _context() -> ToolContext:
    return ToolContext(action_id=uuid4(), step_id=uuid4())


def test_inspect_screen_returns_compact_visual_understanding() -> None:
    capture = FakeCapture()
    vision = FakeVision()
    tool = InspectScreenTool(capture, vision, max_image_dimension=1600)

    result = tool.execute(
        InspectScreenArguments(query="What is this installer waiting for?"),
        _context(),
    )

    assert result.summary == "An installer is ready."
    assert result.visible_text == ["Ready to Install", "Install", "Cancel"]
    assert result.relevant_elements[0].label == "Install"
    assert result.window_title == "Setup"
    assert result.model == "qwen3.5:test"
    assert capture.scopes == ["active_window"]


def test_visual_click_maps_normalized_coordinates_internally() -> None:
    capture = FakeCapture()
    vision = FakeVision(confidence=0.91)
    pointer = FakePointer()
    tool = ActivateVisualTargetTool(
        capture,
        vision,
        pointer,
        max_image_dimension=1600,
        minimum_confidence=0.70,
    )

    result = tool.execute(
        ActivateVisualTargetArguments(query="the Continue button"),
        _context(),
    )

    # 75% across a 1000 px-wide frame, 80% down a 500 px-high frame.
    assert pointer.clicks == [(850, 600)]
    assert result.success is True
    assert result.verified is False
    assert result.warnings
    assert result.confidence == 0.91
    assert tool.confirmation is ConfirmationMode.CONDITIONAL


def test_visual_click_refuses_low_confidence_target() -> None:
    tool = ActivateVisualTargetTool(
        FakeCapture(),
        FakeVision(confidence=0.41),
        FakePointer(),
        max_image_dimension=1600,
        minimum_confidence=0.70,
    )

    with pytest.raises(ToolExecutionError) as error:
        tool.execute(
            ActivateVisualTargetArguments(query="the tiny unlabeled icon"),
            _context(),
        )

    assert error.value.code == "VISUAL_TARGET_NOT_RELIABLE"


def test_visual_click_uses_uia_only_after_low_confidence_vision() -> None:
    capture = FakeCapture()
    pointer = FakePointer()
    uia = FakeUIA()
    tool = ActivateVisualTargetTool(
        capture,
        FakeVision(confidence=0.41),
        pointer,
        max_image_dimension=1600,
        minimum_confidence=0.70,
        uia_fallback=uia,
    )

    result = tool.execute(
        ActivateVisualTargetArguments(query="the Continue button"),
        _context(),
    )

    assert pointer.clicks == []
    assert uia.clicks == ["dui_fake_continue"]
    assert uia.inspections[0] == "Continue"
    assert result.success is True
    assert result.verified is False
    assert result.model == "windows-uia-fallback"


def test_inspect_screen_uses_uia_only_when_vision_fails() -> None:
    uia = FakeUIA()
    tool = InspectScreenTool(
        FakeCapture(),
        BrokenVision(),
        max_image_dimension=1600,
        uia_fallback=uia,
    )

    result = tool.execute(
        InspectScreenArguments(query="What controls are visible?"),
        _context(),
    )

    assert result.model == "windows-uia-fallback"
    assert result.window_title == "Setup"
    assert result.relevant_elements[0].label == "Continue"
