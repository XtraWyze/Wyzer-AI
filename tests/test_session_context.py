import json
from datetime import UTC, datetime
from uuid import uuid4

from wyzer.conversation import SessionContextManager
from wyzer.models import StructuredError, ToolResult, WorldStateSnapshot


def result(
    tool: str,
    *,
    ok: bool = True,
    data: dict[str, object] | None = None,
    evidence: dict[str, object] | None = None,
    error: StructuredError | None = None,
) -> ToolResult:
    now = datetime.now(UTC)
    return ToolResult(
        ok=ok,
        tool=tool,
        action_id=uuid4(),
        step_id=uuid4(),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        data=data,
        evidence=evidence or {},
        error=error,
    )


def window(handle: int, title: str, application: str, monitor_id: str) -> dict[str, object]:
    return {
        "handle": handle,
        "title": title,
        "process_id": handle + 100,
        "application": application,
        "monitor_id": monitor_id,
        "minimized": False,
        "maximized": False,
    }


def world() -> WorldStateSnapshot:
    return WorldStateSnapshot(
        monitor_layout=[
            {"monitor_id": "monitor:internal-a", "number": 1, "label": "monitor 1"},
            {"monitor_id": "monitor:internal-b", "number": 2, "label": "monitor 2"},
        ]
    )


def test_window_continuity_keeps_identity_and_monitor_history() -> None:
    manager = SessionContextManager()
    notepad = window(44, "Untitled - Notepad", "notepad.exe", "monitor:internal-a")
    moved = {**notepad, "monitor_id": "monitor:internal-b"}

    manager.record_tool_result(
        result(
            "open_application",
            data={"application": "Notepad", "window": notepad, "verified": True},
            evidence={"verification_status": "verified"},
        ),
        {"application": "Notepad"},
        after=world(),
    )
    manager.record_tool_result(
        result(
            "move_named_window_to_monitor",
            data={
                "target": "Notepad",
                "window": moved,
                "verified": True,
                "source_monitor": {
                    "monitor_id": "monitor:internal-a",
                    "number": 1,
                    "label": "monitor 1",
                },
                "target_monitor": {
                    "monitor_id": "monitor:internal-b",
                    "number": 2,
                    "label": "monitor 2",
                },
            },
            evidence={"verification_status": "verified"},
        ),
        {"window": "Notepad", "destination": {"relation": "other"}},
        before=world(),
        after=world(),
    )
    for action in ("minimize", "restore"):
        manager.record_tool_result(
            result(
                "control_named_window",
                data={"target": "Notepad", "window": moved, "verified": True},
                evidence={"verification_status": "verified"},
            ),
            {"window": "Notepad", "action": action},
            after=world(),
        )

    snapshot = manager.snapshot()
    assert snapshot.active_window is not None
    assert snapshot.active_window.handle == 44
    assert snapshot.active_window.name == "Notepad"
    assert snapshot.last_monitor is not None
    assert snapshot.last_monitor.number == 2
    assert snapshot.previous_monitor is not None
    assert snapshot.previous_monitor.number == 1


def test_previous_window_tracks_distinct_successful_targets() -> None:
    manager = SessionContextManager()
    for handle, name, executable in (
        (41, "Notepad", "notepad.exe"),
        (42, "Calculator", "CalculatorApp.exe"),
    ):
        manager.record_tool_result(
            result(
                "open_application",
                data={
                    "application": name,
                    "window": window(handle, name, executable, "monitor:internal-a"),
                    "verified": True,
                },
                evidence={"verification_status": "verified"},
            ),
            {"application": name},
            after=world(),
        )

    snapshot = manager.snapshot()
    assert snapshot.active_window is not None
    assert snapshot.active_window.name == "Calculator"
    assert snapshot.previous_window is not None
    assert snapshot.previous_window.name == "Notepad"
    assert [entity.name for entity in snapshot.recent_entities] == ["Notepad", "Calculator"]


def test_file_search_then_verified_open_sets_last_file() -> None:
    manager = SessionContextManager()
    path = r"C:\Users\Example\WyzerNext\wyzer\app\orchestrator.py"
    manager.record_tool_result(
        result(
            "search_files",
            data={"query": "orchestrator.py", "matches": [{"path": path}], "count": 1},
        ),
        {"query": "orchestrator.py"},
    )
    assert manager.snapshot().last_file is None

    manager.record_tool_result(
        result(
            "open_file",
            data={"target": path, "target_kind": "file", "verified": True},
            evidence={"verification_status": "verified"},
        ),
        {"path": path},
    )

    snapshot = manager.snapshot()
    assert snapshot.last_file is not None
    assert snapshot.last_file.path == path
    assert snapshot.current_folder == r"C:\Users\Example\WyzerNext\wyzer\app"


def test_failed_or_unverified_open_does_not_replace_last_file() -> None:
    manager = SessionContextManager()
    good = r"C:\work\README.md"
    manager.record_tool_result(
        result(
            "open_file",
            data={"target": good, "target_kind": "file", "verified": True},
            evidence={"verification_status": "verified"},
        ),
        {"path": good},
    )
    manager.record_tool_result(
        result(
            "open_file",
            ok=False,
            error=StructuredError(
                code="FILE_NOT_FOUND",
                message="missing",
                details={"path": r"C:\missing.txt"},
            ),
        ),
        {"path": r"C:\missing.txt"},
    )
    manager.record_tool_result(
        result(
            "open_file",
            data={
                "target": r"C:\unverified.txt",
                "target_kind": "file",
                "verified": False,
            },
            evidence={"verification_status": "unavailable"},
        ),
        {"path": r"C:\unverified.txt"},
    )

    snapshot = manager.snapshot()
    assert snapshot.last_file is not None
    assert snapshot.last_file.path == good
    assert snapshot.last_tool_result == {
        "tool": "open_file",
        "ok": False,
        "target": r"C:\unverified.txt",
    }


def test_history_and_model_snapshot_are_bounded_and_exclude_raw_payloads() -> None:
    manager = SessionContextManager(history_limit=5)
    huge = "private raw output " * 5_000
    for index in range(20):
        manager.record_tool_result(
            result(
                "read_text_file",
                data={
                    "path": rf"C:\work\file-{index}.txt",
                    "content": huge,
                    "truncated": False,
                },
            ),
            {"path": rf"C:\work\file-{index}.txt"},
        )

    snapshot = manager.snapshot()
    context = manager.model_context(maximum_characters=1_200)
    serialized = json.dumps(context, separators=(",", ":"))
    assert len(snapshot.recent_actions) == 5
    assert len(snapshot.recent_entities) == 5
    assert len(snapshot.recent_files) == 5
    assert len(serialized) <= 1_200
    assert "private raw output" not in serialized
    assert "file-19.txt" in serialized


def test_browser_page_and_tab_use_returned_url_and_index() -> None:
    manager = SessionContextManager()
    manager.record_tool_result(
        result(
            "browser_list_tabs",
            data={
                "tabs": [
                    {"index": 1, "active": False, "title": "One", "url": "https://one.test"},
                    {"index": 2, "active": True, "title": "Two", "url": "https://two.test"},
                ]
            },
        )
    )

    snapshot = manager.snapshot()
    assert snapshot.last_browser_page is not None
    assert snapshot.last_browser_page.url == "https://two.test"
    assert snapshot.last_browser_tab is not None
    assert snapshot.last_browser_tab.tab_index == 2
