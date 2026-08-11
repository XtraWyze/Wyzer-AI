import pytest
from pydantic import Field, ValidationError

from tests.fakes import ConsequentialEchoTool, EchoTool
from wyzer.models import ConfirmationMode, ToolArguments
from wyzer.tools import SimpleToolPack, ToolRegistry
from wyzer.tools.registry import (
    DuplicateToolError,
    UnknownCapabilityError,
    UnknownToolError,
)
from wyzer.tools.schema import model_parameters


class TitleArguments(ToolArguments):
    title: str = Field(description="A real argument named title.")


def test_registry_rejects_duplicates() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(DuplicateToolError):
        registry.register(EchoTool())


def test_registry_rejects_unknown_lookup() -> None:
    with pytest.raises(UnknownToolError):
        ToolRegistry().get("imaginary")


def test_registry_validates_arguments_and_exposes_manifest() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    arguments = registry.validate_arguments("echo", {"message": "hello"})
    assert arguments.model_dump() == {"message": "hello"}
    with pytest.raises(ValidationError):
        registry.validate_arguments("echo", {"wrong": "value"})
    assert registry.compact_manifest()[0]["name"] == "echo"
    native = registry.native_tools()[0]
    assert native.function.name == "echo"
    assert native.function.parameters == model_parameters(EchoTool.arguments_type)


def test_compact_model_schema_preserves_an_argument_named_title() -> None:
    parameters = model_parameters(TitleArguments)

    assert "title" in parameters["properties"]
    assert parameters["required"] == ["title"]


def test_model_view_can_only_activate_registered_capability_packs() -> None:
    registry = ToolRegistry()
    registry.register_pack(SimpleToolPack("special", (EchoTool,)), default_visible=False)

    assert registry.native_tools() == []
    assert registry.model_view(("special",)).tool_names == ("echo",)
    with pytest.raises(UnknownCapabilityError):
        registry.model_view(("imaginary",))


def test_capability_manifest_explains_what_optional_pack_does() -> None:
    registry = ToolRegistry()
    registry.register_pack(
        SimpleToolPack("special", (EchoTool,), "Handle specialized echo operations."),
        default_visible=False,
    )

    assert registry.capability_manifest() == [
        {
            "name": "special",
            "description": "Handle specialized echo operations.",
            "tool_count": 1,
            "active": False,
        }
    ]


def test_capability_coordination_tools_come_first_in_model_view() -> None:
    from wyzer.tools import create_default_registry

    names = create_default_registry().model_view().tool_names

    assert names[:6] == (
        "activate_clipboard_tools",
        "activate_desktop_interaction_tools",
        "activate_diagnostics_tools",
        "activate_file_tools",
        "activate_managed_browser_tools",
        "activate_screen_perception_tools",
    )


def test_generated_activators_are_zero_argument_metadata_mappings() -> None:
    from wyzer.tools import create_default_registry

    registry = create_default_registry()
    activators = {
        tool.function.name: tool
        for tool in registry.native_tools()
        if registry.activation_capability(tool.function.name) is not None
    }

    assert registry.activation_capability("activate_managed_browser_tools") == "browser"
    assert registry.activation_capability("activate_file_tools") == "files"
    assert all(tool.function.parameters["properties"] == {} for tool in activators.values())
    assert all("required" not in tool.function.parameters for tool in activators.values())
    assert all("Does not perform the user's action" in tool.function.description for tool in activators.values())
    assert "list_tool_capabilities" not in registry.model_view().tool_names
    assert "activate_tool_capability" not in registry.model_view().tool_names


def test_capability_view_preserves_visibility_confirmation_and_validation() -> None:
    class HiddenEchoTool(EchoTool):
        name = "hidden_echo"
        llm_visible = False

    registry = ToolRegistry()
    registry.register_pack(
        SimpleToolPack("special", (ConsequentialEchoTool, HiddenEchoTool)),
        default_visible=False,
    )

    view = registry.model_view(("special",))
    assert view.tool_names == ("send_message",)
    assert registry.get("send_message").confirmation == ConfirmationMode.ALWAYS
    with pytest.raises(ValidationError):
        registry.validate_arguments("send_message", {"wrong": "value"})
