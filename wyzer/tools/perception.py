"""Vision-first screen perception with an internal UI Automation fallback.

The LLM never receives screenshots, raw coordinates, window handles, or UIA
references. Screen images are sent only to the configured local Ollama vision
model. Vision is the primary desktop perception/targeting path. Windows UI
Automation is used internally only when vision is unavailable or cannot resolve
an active-window target reliably.
"""

from __future__ import annotations

import ctypes
import platform
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments
from wyzer.perception import (
    OllamaVisionClient,
    PillowScreenCapture,
    VisionClient,
    VisionClientError,
)
from wyzer.perception.screen import ScreenCapture
from wyzer.tools.base import Tool, ToolContext, ToolExecutionError
from wyzer.tools.desktop_interaction import DesktopUIAdapter, PywinautoDesktopAdapter

ScreenScope = Literal["active_window", "full_desktop"]


class InspectScreenArguments(ToolArguments):
    query: str = Field(
        default="Describe the visible screen and anything important or actionable.",
        min_length=1,
        max_length=500,
        description="What to determine from the visible screen.",
    )
    scope: ScreenScope = Field(
        default="active_window",
        description="Focused window or full desktop.",
    )


class ActivateVisualTargetArguments(ToolArguments):
    query: str = Field(
        min_length=1,
        max_length=240,
        description=(
            "Visible target, e.g. 'the Install button'."
        ),
    )
    action: Literal["click"] = Field(
        default="click",
        description="Action to perform.",
    )
    scope: ScreenScope = Field(
        default="active_window",
        description="Focused window or full desktop.",
    )


class VisualElement(BaseModel):
    label: str
    kind: str
    state: str | None = None


class ScreenInspectionResult(BaseModel):
    summary: str
    visible_text: list[str] = Field(default_factory=list)
    relevant_elements: list[VisualElement] = Field(default_factory=list)
    window_title: str | None = None
    scope: ScreenScope
    model: str
    warnings: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class VisualActionResult(BaseModel):
    action: Literal["click"] = "click"
    target: str
    success: bool
    verified: bool = False
    model: str
    confidence: float = Field(ge=0, le=1)
    description: str = ""
    window_title: str | None = None
    warnings: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class PointerController(Protocol):
    available: bool
    unavailable_reason: str | None

    def click(self, x: int, y: int) -> None: ...


class WindowsPointerController:
    def __init__(self) -> None:
        self.available = platform.system() == "Windows"
        self.unavailable_reason = (
            None if self.available else "visual clicking is available only on Windows"
        )

    def click(self, x: int, y: int) -> None:
        if not self.available:
            raise RuntimeError(self.unavailable_reason or "pointer control is unavailable")
        user32 = ctypes.windll.user32
        if not user32.SetCursorPos(int(x), int(y)):
            raise RuntimeError("Windows could not move the pointer to the visual target")
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


class InspectScreenTool(Tool[InspectScreenArguments, ScreenInspectionResult]):
    name = "inspect_screen"
    description = (
        "Observe, describe, or read visible non-web Windows screen content, text, and messages. "
        "This never clicks; activate_visual_target clicks a visible target."
    )
    arguments_type = InspectScreenArguments
    result_type = ScreenInspectionResult
    risk_level = RiskLevel.LOW
    read_only = True
    confirmation = ConfirmationMode.NEVER
    default_timeout_seconds = 60.0

    def __init__(
        self,
        capture: ScreenCapture,
        vision: VisionClient,
        *,
        max_image_dimension: int,
        uia_fallback: DesktopUIAdapter | None = None,
    ) -> None:
        self.capture = capture
        self.vision = vision
        self.max_image_dimension = max_image_dimension
        self.uia_fallback = uia_fallback
        vision_path = bool(capture.available and vision.available)
        fallback_path = bool(uia_fallback is not None and uia_fallback.available)
        self.available = vision_path or fallback_path
        reasons = [
            reason
            for reason in (
                capture.unavailable_reason,
                vision.unavailable_reason,
                getattr(uia_fallback, "unavailable_reason", None),
            )
            if reason
        ]
        self.unavailable_reason = (
            None if self.available else ("; ".join(reasons) or "screen perception is unavailable")
        )

    def execute(
        self,
        arguments: InspectScreenArguments,
        context: ToolContext,
    ) -> ScreenInspectionResult:
        del context
        try:
            frame = self.capture.capture(arguments.scope, max_dimension=self.max_image_dimension)
            raw = self.vision.analyze(frame.image_bytes, arguments.query)
        except Exception as error:
            fallback = self._uia_inspect(arguments, error)
            if fallback is not None:
                return fallback
            if isinstance(error, VisionClientError):
                raise ToolExecutionError(
                    "VISION_REQUEST_FAILED",
                    str(error),
                    retryable=True,
                ) from error
            raise ToolExecutionError(
                "SCREEN_CAPTURE_FAILED",
                f"I couldn't inspect the screen: {error}",
                retryable=True,
            ) from error

        elements: list[VisualElement] = []
        for item in raw.get("relevant_elements", [])[:30]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if not label or not kind:
                continue
            state = item.get("state")
            elements.append(
                VisualElement(
                    label=label,
                    kind=kind,
                    state=str(state) if state is not None else None,
                )
            )
        visible_text = [
            str(item).strip() for item in raw.get("visible_text", [])[:40] if str(item).strip()
        ]
        warnings = [str(item).strip() for item in raw.get("warnings", [])[:10] if str(item).strip()]
        summary = str(raw.get("summary") or "").strip() or "No useful visual summary was returned."
        return ScreenInspectionResult(
            summary=summary,
            visible_text=visible_text,
            relevant_elements=elements,
            window_title=frame.window_title,
            scope=arguments.scope,
            model=self.vision.model,
            warnings=warnings,
            evidence={
                "verification_status": "verified",
                "predicate": "screen_image_analyzed_by_local_vision_model",
                "observed": {
                    "scope": arguments.scope,
                    "window_title": frame.window_title,
                    "source_size": [frame.source_width, frame.source_height],
                    "sent_size": [frame.sent_width, frame.sent_height],
                    "model": self.vision.model,
                    "perception_path": "vision",
                },
            },
        )

    def _uia_inspect(
        self,
        arguments: InspectScreenArguments,
        vision_error: Exception,
    ) -> ScreenInspectionResult | None:
        if (
            arguments.scope != "active_window"
            or self.uia_fallback is None
            or not self.uia_fallback.available
        ):
            return None
        try:
            result = self.uia_fallback.inspect(_best_uia_query(arguments.query), 40)
        except Exception:
            return None
        rows = [element for element in result.elements if element.visible]
        elements = [
            VisualElement(
                label=element.name,
                kind=element.control_type,
                state="enabled" if element.enabled else "disabled",
            )
            for element in rows[:30]
        ]
        visible_text = [element.name for element in rows[:40] if element.name]
        summary = (
            f"Vision was unavailable, so Windows UI Automation inspected '{result.window_title}'. "
            f"It found {len(rows)} matching visible control{'s' if len(rows) != 1 else ''}."
        )
        return ScreenInspectionResult(
            summary=summary,
            visible_text=visible_text,
            relevant_elements=elements,
            window_title=result.window_title,
            scope="active_window",
            model="windows-uia-fallback",
            warnings=[f"Vision failed and UI Automation was used as a fallback: {vision_error}"],
            evidence={
                "verification_status": "verified",
                "predicate": "focused_window_inspected_by_uia_fallback",
                "observed": {
                    "window_title": result.window_title,
                    "element_count": len(rows),
                    "perception_path": "uia_fallback",
                },
            },
        )


class ActivateVisualTargetTool(Tool[ActivateVisualTargetArguments, VisualActionResult]):
    name = "activate_visual_target"
    description = (
        "Click a named visible non-web Windows button or target. Call this directly when the user "
        "describes the target; do not inspect first."
    )
    arguments_type = ActivateVisualTargetArguments
    result_type = VisualActionResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.CONDITIONAL
    default_timeout_seconds = 60.0

    def __init__(
        self,
        capture: ScreenCapture,
        vision: VisionClient,
        pointer: PointerController,
        *,
        max_image_dimension: int,
        minimum_confidence: float,
        uia_fallback: DesktopUIAdapter | None = None,
    ) -> None:
        self.capture = capture
        self.vision = vision
        self.pointer = pointer
        self.max_image_dimension = max_image_dimension
        self.minimum_confidence = minimum_confidence
        self.uia_fallback = uia_fallback
        vision_path = bool(capture.available and vision.available and pointer.available)
        fallback_path = bool(uia_fallback is not None and uia_fallback.available)
        self.available = vision_path or fallback_path
        reasons = [
            reason
            for reason in (
                capture.unavailable_reason,
                vision.unavailable_reason,
                pointer.unavailable_reason,
                getattr(uia_fallback, "unavailable_reason", None),
            )
            if reason
        ]
        self.unavailable_reason = (
            None
            if self.available
            else ("; ".join(reasons) or "desktop target activation is unavailable")
        )

    def execute(
        self,
        arguments: ActivateVisualTargetArguments,
        context: ToolContext,
    ) -> VisualActionResult:
        del context
        frame = None
        located: dict[str, Any] | None = None
        vision_error: Exception | None = None
        try:
            frame = self.capture.capture(arguments.scope, max_dimension=self.max_image_dimension)
            located = self.vision.locate(frame.image_bytes, arguments.query)
        except Exception as error:
            vision_error = error

        if located is not None and frame is not None:
            found = bool(located.get("found"))
            confidence = _bounded_confidence(located.get("confidence"))
            description = str(located.get("description") or "").strip()
            x = located.get("x")
            y = located.get("y")
            reliable = (
                found
                and isinstance(x, int)
                and not isinstance(x, bool)
                and isinstance(y, int)
                and not isinstance(y, bool)
                and confidence >= self.minimum_confidence
            )
            if reliable:
                assert isinstance(x, int) and not isinstance(x, bool)
                assert isinstance(y, int) and not isinstance(y, bool)
                screen_x, screen_y = frame.point_from_normalized(x, y)
                try:
                    self.pointer.click(screen_x, screen_y)
                except Exception as error:
                    vision_error = error
                else:
                    return VisualActionResult(
                        target=arguments.query,
                        success=True,
                        verified=False,
                        model=self.vision.model,
                        confidence=confidence,
                        description=description,
                        window_title=frame.window_title,
                        warnings=[
                            "The pointer click was sent, but the resulting application state was not verified."
                        ],
                        evidence={
                            "verification_status": "not_verified",
                            "predicate": "vision_guided_click_requested",
                            "observed": {
                                "target": arguments.query,
                                "scope": arguments.scope,
                                "confidence": confidence,
                                "window_title": frame.window_title,
                                "interaction_path": "vision",
                            },
                        },
                    )
            else:
                vision_error = ToolExecutionError(
                    "VISUAL_TARGET_NOT_RELIABLE",
                    f"Vision could not locate '{arguments.query}' reliably enough.",
                    retryable=True,
                    details={
                        "confidence": confidence,
                        "minimum_confidence": self.minimum_confidence,
                        "description": description,
                    },
                )

        fallback = self._uia_click(arguments, vision_error)
        if fallback is not None:
            return fallback

        if isinstance(vision_error, ToolExecutionError):
            raise vision_error
        if isinstance(vision_error, VisionClientError):
            raise ToolExecutionError(
                "VISION_REQUEST_FAILED",
                str(vision_error),
                retryable=True,
            ) from vision_error
        if vision_error is not None:
            raise ToolExecutionError(
                "VISUAL_CLICK_FAILED",
                f"I couldn't activate '{arguments.query}': {vision_error}",
                retryable=True,
            ) from vision_error
        raise ToolExecutionError(
            "VISUAL_TARGET_NOT_RELIABLE",
            f"I couldn't locate '{arguments.query}' reliably enough to click it.",
            retryable=True,
        )

    def _uia_click(
        self,
        arguments: ActivateVisualTargetArguments,
        vision_error: Exception | None,
    ) -> VisualActionResult | None:
        if (
            arguments.scope != "active_window"
            or self.uia_fallback is None
            or not self.uia_fallback.available
        ):
            return None

        for candidate in _uia_query_candidates(arguments.query):
            try:
                inspected = self.uia_fallback.inspect(candidate, 20)
            except Exception:
                continue
            matches = [
                element for element in inspected.elements if element.visible and element.enabled
            ]
            selected = _select_uia_element(matches, candidate)
            if selected is None:
                continue
            try:
                self.uia_fallback.click(selected.element_id)
            except Exception:
                continue
            exact = selected.name.casefold().strip() == candidate.casefold().strip()
            confidence = 1.0 if exact else 0.80
            warning = "Vision could not complete the click, so Windows UI Automation was used as a fallback."
            if vision_error is not None:
                warning += f" Vision reason: {vision_error}"
            return VisualActionResult(
                target=arguments.query,
                success=True,
                verified=False,
                model="windows-uia-fallback",
                confidence=confidence,
                description=f"Activated the UIA control '{selected.name}'.",
                window_title=inspected.window_title,
                warnings=[warning],
                evidence={
                    "verification_status": "not_verified",
                    "predicate": "uia_fallback_click_requested",
                    "observed": {
                        "target": arguments.query,
                        "matched_control": selected.name,
                        "window_title": inspected.window_title,
                        "interaction_path": "uia_fallback",
                    },
                },
            )
        return None


def _bounded_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _best_uia_query(query: str) -> str | None:
    candidates = _uia_query_candidates(query)
    return candidates[0] if candidates else None


def _uia_query_candidates(query: str) -> list[str]:
    raw = " ".join(query.strip().split())
    if not raw:
        return []
    simplified = raw
    simplified = re.sub(
        r"^(?:please\s+)?(?:click|press|select|choose|open|activate|tap)\s+",
        "",
        simplified,
        flags=re.IGNORECASE,
    )
    simplified = re.sub(r"^(?:the|a|an)\s+", "", simplified, flags=re.IGNORECASE)
    simplified = re.sub(
        r"\s+(?:button|link|checkbox|radio button|menu item|tab|icon|control)$",
        "",
        simplified,
        flags=re.IGNORECASE,
    ).strip()
    candidates: list[str] = []
    for item in (simplified, raw):
        if item and item.casefold() not in {existing.casefold() for existing in candidates}:
            candidates.append(item)
    return candidates


def _select_uia_element(elements: list[Any], candidate: str) -> Any | None:
    if not elements:
        return None
    needle = candidate.casefold().strip()
    exact = [element for element in elements if element.name.casefold().strip() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(elements) == 1:
        return elements[0]
    contained = [element for element in elements if needle and needle in element.name.casefold()]
    if len(contained) == 1:
        return contained[0]
    return None


@dataclass(frozen=True, slots=True)
class PerceptionToolPack:
    options: dict[str, object]
    name: str = "perception"
    description: str = (
        "non-web visible screen text/messages/errors and named visible button/target clicks; "
        "inspect_screen observes and activate_visual_target clicks."
    )
    activation_name: str = "screen_perception"
    capture: ScreenCapture | None = None
    vision: VisionClient | None = None
    pointer: PointerController | None = None
    uia_fallback: DesktopUIAdapter | None = None

    def create_tools(self) -> tuple[Tool[Any, Any], ...]:
        enabled = bool(self.options.get("enabled", True))
        provider = str(self.options.get("provider") or "")
        endpoint = str(self.options.get("endpoint") or "")
        model = str(self.options.get("model") or "")
        capture = self.capture or PillowScreenCapture()
        vision: VisionClient
        if self.vision is not None:
            vision = self.vision
        elif provider != "ollama":
            vision = _UnavailableVisionClient(
                model=model,
                reason="screen perception currently requires the Ollama provider",
            )
        else:
            vision = OllamaVisionClient(
                endpoint,
                model,
                timeout_seconds=float(str(self.options.get("vision_timeout_seconds", 45.0))),
                temperature=float(str(self.options.get("temperature", 0.0))),
                think=bool(self.options.get("think", False)),
                keep_alive=str(self.options.get("keep_alive", "30m")),
                enabled=enabled,
            )
        pointer = self.pointer or WindowsPointerController()
        uia_fallback = self.uia_fallback or PywinautoDesktopAdapter()
        max_dimension = int(str(self.options.get("max_image_dimension", 1600)))
        minimum_confidence = float(str(self.options.get("visual_click_min_confidence", 0.70)))
        return (
            InspectScreenTool(
                capture,
                vision,
                max_image_dimension=max_dimension,
                uia_fallback=uia_fallback,
            ),
            ActivateVisualTargetTool(
                capture,
                vision,
                pointer,
                max_image_dimension=max_dimension,
                minimum_confidence=minimum_confidence,
                uia_fallback=uia_fallback,
            ),
        )


class _UnavailableVisionClient:
    available = False

    def __init__(self, model: str, reason: str) -> None:
        self.model = model
        self.unavailable_reason: str | None = reason

    def analyze(self, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        del image_bytes, prompt
        raise VisionClientError(self.unavailable_reason)

    def locate(self, image_bytes: bytes, target: str) -> dict[str, Any]:
        del image_bytes, target
        raise VisionClientError(self.unavailable_reason)


def create_perception_pack(options: dict[str, object]) -> PerceptionToolPack:
    return PerceptionToolPack(options=options)
