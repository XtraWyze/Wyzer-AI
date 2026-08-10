import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import tool_response
from wyzer.brain import FakeChatProvider
from wyzer.config import WyzerSettings
from wyzer.model_acceptance import AcceptanceCase, evaluate, load_cases

CASES_PATH = Path(__file__).parents[1] / "evals" / "model_acceptance.json"


def test_model_acceptance_cases_are_valid_and_cover_key_routes() -> None:
    cases = load_cases(CASES_PATH)

    assert len(cases) >= 25
    expected_routes = {tool for case in cases for tool in case.expected_tools}
    assert {
        "task_plan_create",
        "open_application",
        "browser_search_web",
        "inspect_screen",
        "search_files",
    } <= expected_routes
    assert any(not case.expected_tools for case in cases)
    assert any("browser" in case.prompt.casefold() and not case.expected_tools for case in cases)


def test_model_acceptance_case_ids_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    case = {"case_id": "duplicate", "prompt": "Hello", "expected_tools": []}
    path.write_text(json.dumps([case, case]), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_cases(path)


def test_acceptance_simulates_discovery_activation_and_final_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeChatProvider(
        [
            tool_response(("list_tool_capabilities", {})),
            tool_response(("activate_tool_capability", {"name": "browser"})),
            tool_response(("browser_search_web", {"query": "local models"})),
        ]
    )
    monkeypatch.setattr("wyzer.model_acceptance.create_chat_provider", lambda *args: provider)
    settings = WyzerSettings.load(CASES_PATH.parents[1] / "wyzer.toml")

    report = asyncio.run(
        evaluate(
            settings,
            [
                AcceptanceCase(
                    case_id="scoped_browser",
                    prompt="Search the web for local models.",
                    expected_tools=["browser_search_web"],
                )
            ],
            minimum_pass_rate=1.0,
        )
    )

    assert report.pass_rate == 1.0
    assert report.results[0].tool_trace == [
        "list_tool_capabilities",
        "activate_tool_capability",
        "browser_search_web",
    ]
