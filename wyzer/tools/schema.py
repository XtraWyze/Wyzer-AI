"""Model-facing schema helpers derived from authoritative Pydantic models."""

from __future__ import annotations

from typing import Any, cast

from wyzer.models import ToolArguments

_NAMED_SCHEMA_MAPS = {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}


def model_parameters(arguments_type: type[ToolArguments]) -> dict[str, Any]:
    """Return the argument schema without redundant Pydantic display titles."""

    return cast(dict[str, Any], _without_titles(arguments_type.model_json_schema()))


def _without_titles(value: Any) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key == "title":
                continue
            if key in _NAMED_SCHEMA_MAPS and isinstance(item, dict):
                compact[key] = {name: _without_titles(schema) for name, schema in item.items()}
            else:
                compact[key] = _without_titles(item)
        return compact
    if isinstance(value, list):
        return [_without_titles(item) for item in value]
    return value
