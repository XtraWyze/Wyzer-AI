"""Reusable tool-pack contracts and adapters.

A tool pack is a small factory that creates a related group of tools. Packs are
created separately in every worker process, so package-backed tools do not need
to be pickled or shared between processes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments
from wyzer.tools.base import Tool, ToolContext

ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)
ResultDataT = TypeVar("ResultDataT", bound=BaseModel)

ToolFactory = Callable[[], Tool[Any, Any]]
ToolHandler = Callable[[ArgumentsT, ToolContext], ResultDataT]


@runtime_checkable
class ToolPack(Protocol):
    """Factory contract for one related group of LLM-callable tools."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def activation_name(self) -> str: ...

    def create_tools(self) -> Iterable[Tool[Any, Any]]:
        """Create fresh tool instances for one registry/worker."""


ToolPackFactory = Callable[[], ToolPack]


@dataclass(frozen=True, slots=True)
class SimpleToolPack:
    """A pack assembled from zero-argument tool factories."""

    name: str
    tool_factories: tuple[ToolFactory, ...]
    description: str = ""
    activation_name: str = ""

    def create_tools(self) -> tuple[Tool[Any, Any], ...]:
        return tuple(factory() for factory in self.tool_factories)


class CallableTool(Tool[ArgumentsT, ResultDataT], Generic[ArgumentsT, ResultDataT]):
    """Adapt a normal typed Python function into a Wyzer tool.

    The handler should be a module-level function when worker isolation is enabled.
    External packs are imported inside each spawned worker, so module-level handlers
    remain spawn-safe without custom serialization.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        arguments_type: type[ArgumentsT],
        result_type: type[ResultDataT],
        handler: ToolHandler[ArgumentsT, ResultDataT],
        risk_level: RiskLevel,
        read_only: bool,
        confirmation: ConfirmationMode = ConfirmationMode.NEVER,
        default_timeout_seconds: float = 15.0,
        available: bool = True,
        unavailable_reason: str | None = None,
        llm_visible: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.arguments_type = arguments_type
        self.result_type = result_type
        self.risk_level = risk_level
        self.read_only = read_only
        self.confirmation = confirmation
        self.default_timeout_seconds = default_timeout_seconds
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.llm_visible = llm_visible
        self._handler = handler

    def execute(self, arguments: ArgumentsT, context: ToolContext) -> ResultDataT:
        return self._handler(arguments, context)
