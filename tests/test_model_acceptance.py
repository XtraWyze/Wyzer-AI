import json
from pathlib import Path

import pytest

from wyzer.model_acceptance import load_cases

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


def test_model_acceptance_case_ids_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    case = {"case_id": "duplicate", "prompt": "Hello", "expected_tools": []}
    path.write_text(json.dumps([case, case]), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_cases(path)
