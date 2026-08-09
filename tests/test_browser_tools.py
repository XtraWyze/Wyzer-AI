from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from wyzer.models import ConfirmationMode
from wyzer.tools.base import ToolContext
from wyzer.tools.browser import (
    BrowserStartArguments,
    HistoryArguments,
    NoArguments,
    OpenUrlArguments,
    SearchWebArguments,
    _active_page,
    _history,
    _stop,
    create_browser_pack,
)


def _context() -> ToolContext:
    return ToolContext(action_id=uuid4(), step_id=uuid4())


def test_builtin_pack_exposes_compact_browser_tool_set() -> None:
    pack = create_browser_pack()
    names = [tool.name for tool in pack.create_tools()]

    assert pack.name == "browser"
    assert names == [
        "browser_start",
        "browser_stop",
        "browser_status",
        "browser_open_url",
        "browser_search_web",
        "browser_inspect_page",
        "browser_click",
        "browser_type_text",
        "browser_press_key",
        "browser_scroll",
        "browser_history",
        "browser_list_tabs",
        "browser_switch_tab",
        "browser_close_tab",
    ]
    assert len(names) == len(set(names))


def test_all_browser_tool_definitions_fit_schema_limits() -> None:
    for tool in create_browser_pack().create_tools():
        assert len(tool.definition().description) <= 240


def test_navigation_rejects_non_web_schemes() -> None:
    with pytest.raises(ValidationError):
        OpenUrlArguments(url="file:///C:/secret.txt")

    with pytest.raises(ValidationError):
        BrowserStartArguments(initial_url="javascript:alert(1)")


def test_navigation_accepts_http_and_about_urls() -> None:
    assert OpenUrlArguments(url="https://example.com").url == "https://example.com"
    assert BrowserStartArguments(initial_url="about:blank").initial_url == "about:blank"


def test_browser_defaults_to_chrome_and_google() -> None:
    assert BrowserStartArguments().browser == "chrome"
    assert SearchWebArguments(query="test").engine == "google"


def test_browser_click_and_type_use_conditional_confirmation() -> None:
    tools = {tool.name: tool for tool in create_browser_pack().create_tools()}
    assert tools["browser_click"].confirmation == ConfirmationMode.CONDITIONAL
    assert tools["browser_type_text"].confirmation == ConfirmationMode.CONDITIONAL


def test_stop_reports_already_closed_without_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    import wyzer.tools.browser as module

    monkeypatch.setattr(module, "_json_version", lambda timeout=0.35: None)
    result = _stop(NoArguments(), object())

    assert result.running is False
    assert result.message == "The managed browser is already closed."


def test_stop_closes_connected_managed_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    import wyzer.tools.browser as module

    state = {"closed": False}

    class FakeBrowser:
        def close(self) -> None:
            state["closed"] = True

    @contextmanager
    def fake_connection(*, auto_start: bool = True):
        assert auto_start is False
        yield object(), FakeBrowser()

    monkeypatch.setattr(
        module,
        "_json_version",
        lambda timeout=0.35: None if state["closed"] else {"Browser": "Chrome/140"},
    )
    monkeypatch.setattr(module, "_connection", fake_connection)

    result = _stop(NoArguments(), object())

    assert state["closed"] is True
    assert result.running is False
    assert result.message == "The managed browser was closed."


def test_active_page_uses_persisted_cdp_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wyzer.tools.browser as module

    class FakePage:
        def __init__(self, target_id: str) -> None:
            self.target_id = target_id

        def evaluate(self, script: str) -> bool:
            del script
            return False

    first = FakePage("first")
    second = FakePage("second")
    browser = type(
        "Browser", (), {"contexts": [type("Context", (), {"pages": [first, second]})()]}
    )()
    marker = tmp_path / "active-target"
    marker.write_text("first", encoding="ascii")
    monkeypatch.setattr(module, "_active_target_path", lambda: marker)
    monkeypatch.setattr(module, "_page_target_id", lambda page: page.target_id)

    assert cast(Any, _active_page(browser)) is first


def test_history_back_uses_cdp_entry_without_waiting_for_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wyzer.tools.browser as module

    calls: list[tuple[str, object | None]] = []

    class FakeSession:
        def send(self, method: str, params: object | None = None):
            calls.append((method, params))
            if method == "Page.getNavigationHistory":
                return {
                    "currentIndex": 1,
                    "entries": [
                        {"id": 10, "url": "https://example.com/"},
                        {"id": 11, "url": "about:blank"},
                    ],
                }
            return {}

        def detach(self) -> None:
            calls.append(("detach", None))

    session = FakeSession()
    page_context = type("PageContext", (), {"new_cdp_session": lambda self, page: session})()
    page = type("Page", (), {"context": page_context})()

    @contextmanager
    def fake_connection(*, auto_start: bool = True):
        del auto_start
        yield object(), object()

    monkeypatch.setattr(module, "_connection", fake_connection)
    monkeypatch.setattr(module, "_active_page", lambda browser: page)
    monkeypatch.setattr(module, "_remember_active_page", lambda page: None)

    result = _history(HistoryArguments(action="back"), _context())

    assert result.url == "https://example.com/"
    assert ("Page.navigateToHistoryEntry", {"entryId": 10}) in calls
