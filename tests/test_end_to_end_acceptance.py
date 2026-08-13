import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import text_response, tool_response
from wyzer.brain import FakeChatProvider
from wyzer.config import WyzerSettings
from wyzer.end_to_end_acceptance import (
    EndToEndCase,
    evaluate_end_to_end,
    load_end_to_end_cases,
)

CASES_PATH = Path(__file__).parents[1] / "evals" / "model_end_to_end_acceptance.json"
CONFIG_PATH = CASES_PATH.parents[1] / "wyzer.toml"


def _case(case_id: str) -> EndToEndCase:
    return next(case for case in load_end_to_end_cases(CASES_PATH) if case.case_id == case_id)


def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    case: EndToEndCase,
    provider: FakeChatProvider,
):
    monkeypatch.setattr("wyzer.end_to_end_acceptance.create_chat_provider", lambda *args: provider)
    return asyncio.run(
        evaluate_end_to_end(
            WyzerSettings.load(CONFIG_PATH),
            [case],
            minimum_success_rate=1.0,
        )
    )


def test_end_to_end_cases_are_valid_and_cover_required_categories() -> None:
    cases = load_end_to_end_cases(CASES_PATH)
    identifiers = {case.case_id for case in cases}

    assert {
        "simple_open_type_e2e",
        "simple_open_volume_e2e",
        "simple_open_move_e2e",
        "personal_chrome_close_e2e",
        "unqualified_chrome_close_e2e",
        "search_known_file_e2e",
        "read_known_file_e2e",
        "delete_known_file_e2e",
        "click_retry_e2e",
        "open_named_project_folder_e2e",
        "complex_report_chart_plan_e2e",
        "complex_research_document_plan_e2e",
    } <= identifiers
    assert all(case.required_final_outcome for case in cases)
    assert all(case.allowed_tool_paths for case in cases)


def test_end_to_end_case_ids_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    case = _case("move_named_window_e2e").model_dump(mode="json")
    path.write_text(json.dumps([case, case]), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_end_to_end_cases(path)


def test_preliminary_monitor_observation_can_complete_but_lowers_efficiency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeChatProvider(
        [
            tool_response(("get_monitor_layout", {})),
            tool_response(
                (
                    "move_named_window_to_monitor",
                    {"window": "Notepad", "destination": "right"},
                )
            ),
        ]
    )

    report = _evaluate(monkeypatch, _case("move_named_window_e2e"), provider)
    result = report.results[0]

    assert result.passed is True
    assert result.successful_tool_path == [
        "get_monitor_layout",
        "move_named_window_to_monitor",
    ]
    assert result.unnecessary_observations == 1
    assert result.ideal_first_tool is False
    assert result.efficiency_score < 1.0


def test_observation_without_requested_action_is_a_genuine_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeChatProvider([tool_response(("get_monitor_layout", {})), text_response("Done.")])

    report = _evaluate(monkeypatch, _case("move_named_window_e2e"), provider)

    assert report.results[0].passed is False
    assert report.results[0].failure_reason == "model stopped before a required final outcome"


def test_hallucinated_function_is_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate(
        monkeypatch,
        _case("search_known_file_e2e"),
        FakeChatProvider([tool_response(("file_tools", {}))]),
    )

    assert report.results[0].passed is False
    assert report.results[0].failure_reason == "unknown or hallucinated tool: file_tools"


def test_delete_requires_registered_confirmation_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeChatProvider(
        [
            tool_response(("activate_file_tools", {})),
            tool_response(("delete_path", {"path": "C:\\Users\\Public\\old-notes.txt"})),
        ]
    )

    report = _evaluate(monkeypatch, _case("delete_known_file_e2e"), provider)

    assert report.results[0].passed is True
    assert report.results[0].confirmation_boundaries == ["delete_path"]
    assert report.results[0].executed_calls[-1].confirmation_required is True


def test_unqualified_chrome_close_executes_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate(
        monkeypatch,
        _case("unqualified_chrome_close_e2e"),
        FakeChatProvider(
            [tool_response(("control_named_window", {"window": "Chrome", "action": "close"}))]
        ),
    )

    assert report.results[0].passed is True
    assert report.results[0].satisfied_outcome == "unqualified_chrome_closed"
    assert report.results[0].confirmation_boundaries == []
    assert report.results[0].tool_calls == 1


def test_complex_plan_requires_meaningful_distinct_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate(
        monkeypatch,
        _case("complex_report_chart_plan_e2e"),
        FakeChatProvider(
            [
                tool_response(
                    (
                        "task_plan_create",
                        {
                            "goal": "Create and verify a chart from the latest sales report.",
                            "steps": [
                                {
                                    "description": "Locate the latest report.",
                                    "success_criteria": "The latest report is identified.",
                                },
                                {
                                    "description": "Extract the sales values.",
                                    "success_criteria": "The values are available for charting.",
                                },
                                {
                                    "description": "Create and save the Excel chart.",
                                    "success_criteria": "The chart file is saved.",
                                },
                                {
                                    "description": "Verify the saved result.",
                                    "success_criteria": "The chart is reopened and checked.",
                                },
                            ],
                        },
                    )
                )
            ]
        ),
    )

    assert report.results[0].passed is True
    assert report.results[0].satisfied_outcome == "dependency_aware_plan_created"


def test_project_folder_opens_directly_after_file_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeChatProvider(
        [
            tool_response(("activate_file_tools", {})),
            tool_response(("open_indexed_folder", {"query": "WyzerNext"})),
        ]
    )

    report = _evaluate(monkeypatch, _case("open_named_project_folder_e2e"), provider)
    result = report.results[0]

    assert result.passed is True
    assert result.successful_tool_path == [
        "activate_file_tools",
        "open_indexed_folder",
    ]
    assert result.unnecessary_observations == 0
    assert result.efficiency_score == 1.0
