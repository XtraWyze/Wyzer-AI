from __future__ import annotations

import json
from typing import Any, cast

import pytest

from tests.fake_windows import FakeWindowsBackend
from wyzer.brain.prompt import SystemPromptBuilder
from wyzer.desktop.windows_backend import (
    _decorate_monitor_topology,
    _resolve_monitor_destination,
    _translated_window_rectangle,
)
from wyzer.models import (
    ConversationState,
    MonitorDestination,
    MonitorInfo,
    Rect,
    WindowInfo,
    WorldStateSnapshot,
)
from wyzer.tools import ToolExecutionError, create_default_registry


def monitor(
    number: int,
    rectangle: Rect,
    *,
    primary: bool = False,
    name: str | None = None,
) -> MonitorInfo:
    device = rf"\\.\DISPLAY{number}"
    return MonitorInfo(
        monitor_id=device,
        device_name=device,
        rectangle=rectangle,
        work_area=rectangle,
        primary=primary,
        number=number,
        friendly_name=name,
    )


def test_monitor_topology_uses_windows_coordinates_not_monitor_number() -> None:
    displays = _decorate_monitor_topology(
        [
            monitor(1, Rect(left=1920, top=0, right=3840, bottom=1080), primary=True),
            monitor(2, Rect(left=0, top=0, right=1920, bottom=1080)),
            monitor(3, Rect(left=1920, top=-1080, right=3840, bottom=0)),
        ]
    )
    source = next(item for item in displays if item.number == 1)

    left = _resolve_monitor_destination(displays, source, MonitorDestination(relation="left"))
    above = _resolve_monitor_destination(displays, source, MonitorDestination(relation="above"))

    assert left.number == 2
    assert left.relative_position == "left"
    assert above.number == 3
    assert above.relative_position == "above"


def test_other_monitor_is_ambiguous_with_three_displays() -> None:
    displays = _decorate_monitor_topology(
        [
            monitor(1, Rect(left=0, top=0, right=1920, bottom=1080), primary=True),
            monitor(2, Rect(left=-1920, top=0, right=0, bottom=1080)),
            monitor(3, Rect(left=1920, top=0, right=3840, bottom=1080)),
        ]
    )
    source = next(item for item in displays if item.number == 1)

    with pytest.raises(ToolExecutionError) as raised:
        _resolve_monitor_destination(displays, source, MonitorDestination(relation="other"))

    assert raised.value.code == "AMBIGUOUS_MONITOR"


def test_window_translation_preserves_relative_placement() -> None:
    source = Rect(left=0, top=0, right=1920, bottom=1040)
    target = Rect(left=1920, top=0, right=4480, bottom=1400)
    window = Rect(left=560, top=220, right=1360, bottom=820)

    left, top, width, height = _translated_window_rectangle(window, source, target)

    assert width == 800
    assert height == 600
    assert 1920 < left < 4480 - width
    assert 0 < top < 1400 - height


def test_tool_schema_exposes_structured_destination_and_accepts_legacy_monitor() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    definition = registry.get("move_named_window_to_monitor").definition()
    properties = definition.arguments_schema["properties"]

    assert "destination" in properties
    assert "monitor" not in properties

    arguments = registry.validate_arguments(
        "move_named_window_to_monitor",
        {"window": "Notepad", "monitor": "two"},
    )
    move_arguments = cast(Any, arguments)
    assert move_arguments.destination == "two"
    assert move_arguments.resolved_destination() == MonitorDestination(number=2)


def test_prompt_exposes_friendly_topology_without_raw_monitor_handles() -> None:
    raw_id = "monitor:131073"
    topology = monitor(
        2,
        Rect(left=1920, top=0, right=3840, bottom=1080),
        name="Test Display",
    ).model_copy(
        update={
            "monitor_id": raw_id,
            "label": "monitor 2",
            "relative_position": "right",
        }
    )
    window = WindowInfo(
        handle=100,
        title="Notepad",
        process_id=10,
        application="notepad.exe",
        monitor_id=raw_id,
    )
    prompt = SystemPromptBuilder().build(
        WorldStateSnapshot(
            foreground_window=window,
            monitor_layout=[topology.model_dump(mode="json")],
        ),
        ConversationState(recently_referenced_windows=[window]),
    )
    context = json.loads(prompt.split("CONTEXT_JSON=", 1)[1])

    assert raw_id not in prompt
    assert context["foreground_window"]["monitor"] == "monitor 2"
    assert context["monitor_topology"][0]["relative_position"] == "right"


def test_previous_monitor_returns_window_to_its_last_display() -> None:
    backend = FakeWindowsBackend()
    registry = create_default_registry(backend)
    move_tool = registry.get("move_named_window_to_monitor")
    from uuid import uuid4

    from wyzer.tools.base import ToolContext

    context = ToolContext(action_id=uuid4(), step_id=uuid4())
    first = move_tool.execute(
        move_tool.arguments_type.model_validate(
            {"window": "Notes", "destination": {"relation": "right"}}
        ),
        context,
    )
    second = move_tool.execute(
        move_tool.arguments_type.model_validate(
            {"window": "Notes", "destination": {"relation": "previous"}}
        ),
        context,
    )

    assert first.target_monitor is not None and first.target_monitor.number == 2
    assert second.target_monitor is not None and second.target_monitor.number == 1
    assert second.verified is True
