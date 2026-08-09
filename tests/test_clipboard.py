import pytest
from pydantic import ValidationError

from wyzer.tools.clipboard import FocusedWindowArguments, create_clipboard_pack


def test_focused_clipboard_actions_require_target_window() -> None:
    tools = {tool.name: tool for tool in create_clipboard_pack().create_tools()}

    for name in ("copy_selected_text", "paste_clipboard"):
        schema = tools[name].arguments_type.model_json_schema()
        assert schema["required"] == ["target_window"]
        with pytest.raises(ValidationError):
            tools[name].arguments_type.model_validate({})


def test_focused_window_argument_accepts_application_name() -> None:
    assert FocusedWindowArguments(target_window="Notepad").target_window == "Notepad"
