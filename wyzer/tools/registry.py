"""The single authoritative tool registry."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from wyzer.models import (
    NativeFunctionDefinition,
    NativeToolDefinition,
    ToolArguments,
    ToolDefinition,
)
from wyzer.tools.base import Tool
from wyzer.tools.schema import model_parameters

if TYPE_CHECKING:
    from wyzer.tools.packs import ToolPack

_PACK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class DuplicateToolError(ValueError):
    pass


class DuplicateToolPackError(ValueError):
    pass


class InvalidToolPackError(ValueError):
    pass


class UnknownToolError(KeyError):
    pass


class UnavailableToolError(RuntimeError):
    pass


class UnknownCapabilityError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class ModelToolView:
    """An immutable model-visible projection of one authoritative registry."""

    registry: ToolRegistry
    activated_capabilities: tuple[str, ...]

    @property
    def capability_packs(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.registry.default_capabilities) | set(self.activated_capabilities))
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.function.name for tool in self.native_tools())

    def native_tools(self) -> list[NativeToolDefinition]:
        return self.registry._native_tools_for_capabilities(self.capability_packs)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}
        self._packs: dict[str, tuple[str, ...]] = {}
        self._tool_to_pack: dict[str, str | None] = {}
        self._default_capabilities: set[str] = set()

    def register(self, tool: Tool[Any, Any]) -> None:
        """Register one manually constructed tool.

        Prefer ``register_pack`` for normal application composition so the registry
        can report which package supplied every tool.
        """
        self._register_many((tool,), pack_name=None)

    def register_many(self, tools: Iterable[Tool[Any, Any]]) -> None:
        """Atomically register several manually constructed tools."""
        self._register_many(tuple(tools), pack_name=None)

    def register_pack(self, pack: ToolPack, *, default_visible: bool = True) -> None:
        """Create and atomically register all tools from one named pack."""
        pack_name = pack.name.strip()
        if not _PACK_NAME_PATTERN.fullmatch(pack_name):
            raise InvalidToolPackError(
                "tool-pack names must start with a lowercase letter and contain only "
                "lowercase letters, digits, dots, underscores, or hyphens"
            )
        if pack_name in self._packs:
            raise DuplicateToolPackError(f"tool pack already registered: {pack_name}")
        try:
            tools = tuple(pack.create_tools())
        except Exception as error:
            raise InvalidToolPackError(
                f"tool pack {pack_name} could not create tools: {error}"
            ) from error
        if not tools:
            raise InvalidToolPackError(f"tool pack {pack_name} did not create any tools")
        self._register_many(tools, pack_name=pack_name)
        if default_visible:
            self._default_capabilities.add(pack_name)

    def _register_many(
        self,
        tools: tuple[Tool[Any, Any], ...],
        *,
        pack_name: str | None,
    ) -> None:
        definitions = tuple(tool.definition() for tool in tools)
        names = tuple(definition.name for definition in definitions)
        duplicate_batch_names = sorted({name for name in names if names.count(name) > 1})
        if duplicate_batch_names:
            raise DuplicateToolError(
                "tool appears more than once in registration batch: "
                + ", ".join(duplicate_batch_names)
            )
        conflicts = sorted(set(names) & set(self._tools))
        if conflicts:
            raise DuplicateToolError(f"tool already registered: {', '.join(conflicts)}")

        for tool, definition in zip(tools, definitions, strict=True):
            self._tools[definition.name] = tool
            self._tool_to_pack[definition.name] = pack_name
        if pack_name is not None:
            self._packs[pack_name] = names

    def get(self, name: str, *, require_available: bool = True) -> Tool[Any, Any]:
        try:
            tool = self._tools[name]
        except KeyError as error:
            raise UnknownToolError(name) from error
        if require_available and not tool.available:
            reason = tool.unavailable_reason or "optional dependency is unavailable"
            raise UnavailableToolError(f"{name}: {reason}")
        return tool

    def validate_arguments(self, name: str, raw: dict[str, Any]) -> ToolArguments:
        tool = self.get(name)
        try:
            return cast(ToolArguments, tool.arguments_type.model_validate(raw))
        except ValidationError:
            raise

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools[name].definition() for name in sorted(self._tools))

    def compact_manifest(self) -> list[dict[str, Any]]:
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "arguments": definition.arguments_schema,
                "confirmation": definition.confirmation.value,
                "available": definition.available,
                "llm_visible": bool(getattr(self._tools[definition.name], "llm_visible", True)),
                "pack": self._tool_to_pack[definition.name],
            }
            for definition in self.definitions()
        ]

    def pack_manifest(self) -> list[dict[str, Any]]:
        return [
            {
                "name": pack_name,
                "tools": list(self._packs[pack_name]),
                "count": len(self._packs[pack_name]),
            }
            for pack_name in sorted(self._packs)
        ]

    def pack_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._packs))

    @property
    def default_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._default_capabilities))

    def pack_tools(self, pack_name: str) -> tuple[str, ...]:
        try:
            return self._packs[pack_name]
        except KeyError as error:
            raise KeyError(f"unknown tool pack: {pack_name}") from error

    def tool_pack(self, tool_name: str) -> str | None:
        if tool_name not in self._tools:
            raise UnknownToolError(tool_name)
        return self._tool_to_pack[tool_name]

    def model_view(self, activated_capabilities: Iterable[str] = ()) -> ModelToolView:
        activated = tuple(sorted(set(activated_capabilities)))
        unknown = set(activated) - set(self._packs)
        if unknown:
            raise UnknownCapabilityError("unknown capability pack: " + ", ".join(sorted(unknown)))
        return ModelToolView(self, activated)

    def available_capabilities(self) -> tuple[str, ...]:
        """Return packs that contain at least one available, model-visible tool."""
        return tuple(
            pack_name
            for pack_name in sorted(self._packs)
            if any(
                self._tools[name].available
                and bool(getattr(self._tools[name], "llm_visible", True))
                for name in self._packs[pack_name]
            )
        )

    def capability_manifest(
        self, activated_capabilities: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        view = self.model_view(activated_capabilities)
        active = set(view.capability_packs)
        return [
            {
                "name": pack_name,
                "tool_count": sum(
                    self._tools[name].available
                    and bool(getattr(self._tools[name], "llm_visible", True))
                    for name in self._packs[pack_name]
                ),
                "active": pack_name in active,
            }
            for pack_name in self.available_capabilities()
            if pack_name not in self._default_capabilities
        ]

    def native_tools(self) -> list[NativeToolDefinition]:
        """Build the default model-visible native function view."""
        return self.model_view().native_tools()

    def all_native_tools(self) -> list[NativeToolDefinition]:
        """Build all available model-visible definitions for diagnostics and tests."""
        return self._native_tools_for_capabilities(self._packs)

    def _native_tools_for_capabilities(
        self, capabilities: Iterable[str]
    ) -> list[NativeToolDefinition]:
        """Build native definitions from registered schemas for one validated view."""
        capability_set = set(capabilities)
        return [
            NativeToolDefinition(
                function=NativeFunctionDefinition(
                    name=definition.name,
                    description=definition.description,
                    parameters=model_parameters(self._tools[definition.name].arguments_type),
                )
            )
            for definition in self.definitions()
            if definition.available
            and bool(getattr(self._tools[definition.name], "llm_visible", True))
            and (
                self._tool_to_pack[definition.name] is None
                or self._tool_to_pack[definition.name] in capability_set
            )
        ]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)
