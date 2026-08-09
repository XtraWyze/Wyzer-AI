"""NON-FUNCTIONAL EXAMPLE ONLY.

This package intentionally exposes no ``wyzer.tool_packs`` entry point and its
handler always raises. It exists only to show the shape of an external pack.
Do not install or enable it.
"""

from __future__ import annotations

from pydantic import BaseModel

from wyzer.models import RiskLevel, ToolArguments
from wyzer.tools import CallableTool, SimpleToolPack, ToolContext


class ExampleArguments(ToolArguments):
    text: str


class ExampleResult(BaseModel):
    message: str


def _not_implemented(arguments: ExampleArguments, context: ToolContext) -> ExampleResult:
    del arguments, context
    raise RuntimeError("Documentation-only example: this tool is intentionally unusable.")


def create_pack() -> SimpleToolPack:
    return SimpleToolPack(
        "example_only",
        (
            lambda: CallableTool(
                name="example_unusable_tool",
                description="Documentation-only example that intentionally cannot execute.",
                arguments_type=ExampleArguments,
                result_type=ExampleResult,
                handler=_not_implemented,
                risk_level=RiskLevel.LOW,
                read_only=True,
                available=False,
                unavailable_reason="Documentation-only example; intentionally disabled.",
            ),
        ),
    )
