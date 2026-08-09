import pytest
from pydantic import ValidationError

from tests.fakes import EchoTool
from wyzer.tools import ToolRegistry
from wyzer.tools.registry import DuplicateToolError, UnknownToolError


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
    assert native.function.parameters == EchoTool.arguments_type.model_json_schema()
