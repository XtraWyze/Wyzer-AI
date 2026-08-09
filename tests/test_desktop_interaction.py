from uuid import uuid4

from wyzer.models import ConfirmationMode
from wyzer.tools.base import ToolContext
from wyzer.tools.desktop_interaction import (
    DesktopActionResult,
    DesktopInspectionResult,
    TypeDesktopTextArguments,
    TypeDesktopTextTool,
)


class FakeDesktopAdapter:
    available = True
    unavailable_reason = None

    def __init__(self) -> None:
        self.typed: list[str] = []

    def inspect(self, query: str | None, limit: int) -> DesktopInspectionResult:
        del query, limit
        return DesktopInspectionResult(
            window_title="Notes - Notepad",
            application="Notepad.exe",
            elements=[],
        )

    def click(self, element_id: str):  # pragma: no cover - unused here
        raise AssertionError("free typing must not require an element reference")

    def type_text(self, text: str) -> DesktopActionResult:
        self.typed.append(text)
        return DesktopActionResult(action="type_text", target="focused control")

    def press_key(self, key: str, presses: int):  # pragma: no cover - unused here
        raise AssertionError("not used")


def test_type_desktop_text_schema_requires_expected_window() -> None:
    schema = TypeDesktopTextArguments.model_json_schema()

    assert set(schema["properties"]) == {"text", "target_window"}
    assert set(schema["required"]) == {"text", "target_window"}
    assert "element_id" not in schema["properties"]


def test_type_desktop_text_verifies_target_window_without_confirmation() -> None:
    adapter = FakeDesktopAdapter()
    tool = TypeDesktopTextTool(adapter)

    assert tool.confirmation is ConfirmationMode.NEVER

    result = tool.execute(
        TypeDesktopTextArguments(text="hello from Wyzer", target_window="Notepad"),
        ToolContext(action_id=uuid4(), step_id=uuid4()),
    )

    assert adapter.typed == ["hello from Wyzer"]
    assert result.target == "focused control"


def test_type_desktop_text_rejects_changed_focus() -> None:
    from wyzer.tools.base import ToolExecutionError

    adapter = FakeDesktopAdapter()
    tool = TypeDesktopTextTool(adapter)

    try:
        tool.execute(
            TypeDesktopTextArguments(text="hello", target_window="Calculator"),
            ToolContext(action_id=uuid4(), step_id=uuid4()),
        )
    except ToolExecutionError as error:
        assert error.code == "FOCUSED_WINDOW_CHANGED"
    else:  # pragma: no cover - the safety check must fail closed
        raise AssertionError("typing proceeded in the wrong window")
