"""Regression coverage for the semantic model-facing tool API."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from wyzer.tasks.tools import TASK_ARGUMENT_TYPES, task_native_tools
from wyzer.tools import create_default_registry

EXPECTED_MODEL_TOOL_NAMES = {
    "activate_clipboard_tools",
    "activate_desktop_interaction_tools",
    "activate_diagnostics_tools",
    "activate_file_tools",
    "activate_managed_browser_tools",
    "activate_screen_perception_tools",
    "activate_visual_target",
    "browser_click",
    "browser_close_tab",
    "browser_history",
    "browser_inspect_page",
    "browser_list_tabs",
    "browser_open_url",
    "browser_press_key",
    "browser_scroll",
    "browser_search_web",
    "browser_stop",
    "browser_switch_tab",
    "browser_type_text",
    "control_application_audio",
    "control_master_audio",
    "control_media",
    "control_named_window",
    "copy_path",
    "copy_selected_text",
    "create_directory",
    "delete_path",
    "diagnose_system",
    "get_current_media",
    "get_foreground_window",
    "get_monitor_layout",
    "get_system_profile",
    "inspect_screen",
    "is_process_running",
    "list_audio_sessions",
    "list_directory",
    "list_installed_games",
    "list_open_windows",
    "move_named_window_to_monitor",
    "move_path",
    "mute_all_audio_except",
    "open_application",
    "open_file",
    "open_indexed_folder",
    "paste_clipboard",
    "press_desktop_key",
    "read_clipboard",
    "read_text_file",
    "refresh_file_index",
    "rename_path",
    "search_files",
    "search_installed_applications",
    "task_plan_create",
    "task_plan_revise",
    "task_step_update",
    "type_desktop_text",
    "write_clipboard",
}
EXPECTED_SEMANTIC_SCHEMA_SHA256 = "ee4245352768fd740baced57a31417da4b8b2819fa15696ae41996e85dd5437b"


def _semantic_schema(value: Any) -> Any:
    """Remove prose-only keys while retaining every API/validation constraint."""

    if isinstance(value, dict):
        return {
            key: _semantic_schema(item)
            for key, item in sorted(value.items())
            if key not in {"description", "title"}
        }
    if isinstance(value, list):
        return [_semantic_schema(item) for item in value]
    return value


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_model_tool_names_and_semantic_argument_contracts_are_stable() -> None:
    registry = create_default_registry()
    schemas = {
        definition.name: _semantic_schema(definition.arguments_schema)
        for definition in registry.definitions()
        if registry.get(definition.name, require_available=False).llm_visible
    }
    schemas.update(
        {
            name: _semantic_schema(arguments_type.model_json_schema())
            for name, arguments_type in TASK_ARGUMENT_TYPES.items()
        }
    )

    assert set(schemas) == EXPECTED_MODEL_TOOL_NAMES
    serialized = json.dumps(schemas, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(serialized.encode()).hexdigest() == EXPECTED_SEMANTIC_SCHEMA_SHA256


def test_native_tool_schemas_are_json_valid_and_omit_only_display_titles() -> None:
    registry = create_default_registry()
    tools = [*registry.native_tools(), *task_native_tools()]
    expected_available_names = set(registry.model_view().tool_names) | set(TASK_ARGUMENT_TYPES)

    assert {tool.function.name for tool in tools} == expected_available_names
    for tool in tools:
        json.dumps(tool.model_dump(mode="json"))
        assert not _contains_key(tool.function.parameters, "title")
        assert tool.function.parameters.get("type") == "object"


def test_task_tool_view_is_scoped_to_plan_state() -> None:
    assert [tool.function.name for tool in task_native_tools(active_plan=False)] == [
        "task_plan_create"
    ]
    assert [tool.function.name for tool in task_native_tools(active_plan=True)] == [
        "task_step_update",
        "task_plan_revise",
    ]


def test_media_action_schema_explains_skip_direction_to_the_model() -> None:
    tool = next(
        tool
        for tool in create_default_registry().native_tools()
        if tool.function.name == "control_media"
    )
    action = tool.function.parameters["properties"]["action"]

    assert "Use next when the user says skip" in action["description"]


def test_default_tool_descriptions_reject_near_match_fallbacks() -> None:
    registry = create_default_registry()
    tools = {
        tool.function.name: tool.function.description
        for tool in registry.native_tools()
    }
    all_tools = {tool.function.name: tool.function.description for tool in registry.all_native_tools()}

    assert "Never use to find a file" in tools["search_installed_applications"]
    assert "activate the files capability" in all_tools["open_file"]
    assert "never use it to close a browser" in tools["control_media"]
    assert "no preliminary window check is needed" in tools["control_named_window"]
