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
    identifiers = {case.case_id for case in cases}
    assert {
        "open_known_file",
        "identify_focused_window",
        "simple_compound_open_and_type",
        "simple_compound_open_and_move",
        "complex_report_chart_plan",
        "complex_research_document_plan",
        "count_installed_games",
        "list_installed_games_reported",
        "open_named_project_folder",
    } <= identifiers


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
            tool_response(("activate_managed_browser_tools", {})),
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
        "activate_managed_browser_tools",
        "browser_search_web",
    ]


def test_acceptance_allows_multiple_direct_tools_for_simple_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeChatProvider(
        [
            tool_response(("open_application", {"application": "Calculator"})),
            tool_response(("control_master_audio", {"operation": "decrease", "amount": 10})),
        ]
    )
    monkeypatch.setattr("wyzer.model_acceptance.create_chat_provider", lambda *args: provider)
    settings = WyzerSettings.load(CASES_PATH.parents[1] / "wyzer.toml")

    report = asyncio.run(
        evaluate(
            settings,
            [
                AcceptanceCase(
                    case_id="simple_direct_sequence",
                    prompt="Do both small actions.",
                    expected_tools=["open_application", "control_master_audio"],
                    forbidden_tools=["task_plan_create"],
                )
            ],
            minimum_pass_rate=1.0,
        )
    )

    assert report.pass_rate == 1.0
    assert report.results[0].actual_tools == ["open_application", "control_master_audio"]
    assert report.results[0].tool_trace == ["open_application", "control_master_audio"]
