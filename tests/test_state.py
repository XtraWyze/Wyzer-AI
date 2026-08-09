from datetime import UTC, datetime
from uuid import uuid4

import pytest

from wyzer.models import DesktopPerception, ToolResult, WindowInfo
from wyzer.state import WorldStateManager


def test_world_state_updates_only_through_typed_observation() -> None:
    manager = WorldStateManager()
    window = WindowInfo(handle=10, title="Calculator", process_id=42)
    manager.apply_perception(DesktopPerception(foreground_window=window))
    snapshot = manager.snapshot()
    assert snapshot.revision == 1
    assert snapshot.foreground_window == window
    assert snapshot.known_open_windows == [window]
    assert snapshot.desktop_scene.foreground_window == window
    assert snapshot.desktop_scene.sources[-1].name == "desktop_perception"


def test_snapshot_is_immutable_and_detached() -> None:
    manager = WorldStateManager()
    snapshot = manager.snapshot()
    assert snapshot.revision == 0
    manager.replace_windows([WindowInfo(handle=1, title="A", process_id=1)])
    assert snapshot.known_open_windows == []


def test_operating_mode_is_explicit_and_validated() -> None:
    manager = WorldStateManager()

    manager.set_operating_mode("voice")

    assert manager.snapshot().operating_mode == "voice"
    with pytest.raises(ValueError):
        manager.set_operating_mode("silent")


def test_targeted_window_refresh_reconciles_only_the_requested_application() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from wyzer.models import ToolResult

    manager = WorldStateManager()
    manager.replace_windows(
        [
            WindowInfo(
                handle=1, title="Calculator", process_id=10, application="CalculatorApp.exe"
            ),
            WindowInfo(handle=2, title="Notes", process_id=11, application="notepad.exe"),
        ]
    )
    now = datetime.now(UTC)
    result = ToolResult(
        ok=True,
        tool="list_open_windows",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        data={"windows": [], "count": 0, "query": "Calculator"},
    )

    manager.apply_tool_observation(result)

    assert [window.title for window in manager.snapshot().known_open_windows] == ["Notes"]


def test_verified_named_close_removes_closed_windows_from_world_state() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from wyzer.models import ToolResult

    manager = WorldStateManager()
    manager.replace_windows(
        [
            WindowInfo(handle=1, title="Calculator", process_id=10),
            WindowInfo(handle=2, title="Notes", process_id=11),
        ]
    )
    now = datetime.now(UTC)
    result = ToolResult(
        ok=True,
        tool="control_named_window",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        data={"window_handle": 1, "window_handles": [1], "verified": True, "window": None},
        evidence={
            "verification_status": "verified",
            "predicate": "named_window_close",
            "observed": {"window_handle": 1},
        },
    )

    manager.apply_tool_observation(result)

    assert [window.handle for window in manager.snapshot().known_open_windows] == [2]


def test_scene_merges_browser_inspection_and_redacts_sensitive_visible_text() -> None:
    manager = WorldStateManager()
    now = datetime.now(UTC)
    result = ToolResult(
        ok=True,
        tool="browser_inspect_page",
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        data={
            "title": "Account",
            "url": "https://example.test/account",
            "text": "Welcome\nPassword: do-not-share\nBalance",
            "elements": [{"ref": "e1", "role": "button", "name": "Save", "tag": "button"}],
        },
    )

    manager.apply_tool_observation(result)

    scene = manager.snapshot().desktop_scene
    assert scene.browser is not None and scene.browser.active_url == "https://example.test/account"
    assert scene.visible_text == ["Welcome", "[sensitive text hidden]", "Balance"]
    assert scene.redacted_content is True
    assert scene.elements[0].label == "Save"
    assert scene.sources[-1].name == "browser_page"


def test_scene_discards_stale_browser_page_content_after_navigation() -> None:
    manager = WorldStateManager()
    now = datetime.now(UTC)
    manager.apply_tool_observation(
        ToolResult(
            ok=True,
            tool="browser_inspect_page",
            action_id=uuid4(),
            step_id=uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
            data={
                "title": "One",
                "url": "https://example.test/one",
                "text": "Old page",
                "elements": [{"role": "button", "name": "Old", "tag": "button"}],
            },
        )
    )
    manager.apply_tool_observation(
        ToolResult(
            ok=True,
            tool="browser_open_url",
            action_id=uuid4(),
            step_id=uuid4(),
            started_at=now,
            finished_at=now,
            duration_ms=0,
            data={"success": True, "title": "Two", "url": "https://example.test/two"},
        )
    )

    scene = manager.snapshot().desktop_scene
    assert scene.browser is not None and scene.browser.active_url == "https://example.test/two"
    assert scene.visible_text == []
    assert scene.elements == []
    assert any(change.kind == "browser_navigated" for change in scene.recent_changes)
