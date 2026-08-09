"""Explicit discovery of installed Wyzer tool-pack entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from typing import cast

from wyzer.tools.packs import ToolPack

TOOL_PACK_ENTRYPOINT_GROUP = "wyzer.tool_packs"


class ToolPackDiscoveryError(RuntimeError):
    pass


def discover_tool_pack_entry_points() -> dict[str, EntryPoint]:
    """Return installed pack entry points keyed by their configured names."""
    discovered: dict[str, EntryPoint] = {}
    for entry_point in entry_points().select(group=TOOL_PACK_ENTRYPOINT_GROUP):
        if entry_point.name in discovered:
            raise ToolPackDiscoveryError(
                f"duplicate {TOOL_PACK_ENTRYPOINT_GROUP} entry point: {entry_point.name}"
            )
        discovered[entry_point.name] = entry_point
    return discovered


def discover_tool_pack_names() -> tuple[str, ...]:
    return tuple(sorted(discover_tool_pack_entry_points()))


def load_enabled_tool_packs(enabled_names: tuple[str, ...]) -> tuple[ToolPack, ...]:
    """Load only explicitly enabled installed packs.

    Entry points may expose either a ToolPack instance or a zero-argument factory/class
    returning a ToolPack. The entry-point name must match the pack's declared name.
    """
    normalized = tuple(name.strip() for name in enabled_names if name.strip())
    if len(normalized) != len(set(normalized)):
        raise ToolPackDiscoveryError("enabled tool-pack names must be unique")
    if not normalized:
        return ()

    available = discover_tool_pack_entry_points()
    missing = sorted(set(normalized) - set(available))
    if missing:
        names = ", ".join(missing)
        raise ToolPackDiscoveryError(f"enabled tool pack is not installed: {names}")

    packs: list[ToolPack] = []
    for enabled_name in normalized:
        entry_point = available[enabled_name]
        try:
            loaded = entry_point.load()
            should_create = isinstance(loaded, type) or not _looks_like_pack(loaded)
            candidate = loaded() if should_create else loaded
        except Exception as error:
            raise ToolPackDiscoveryError(
                f"failed to load tool pack {enabled_name}: {error}"
            ) from error
        if not _looks_like_pack(candidate):
            raise ToolPackDiscoveryError(f"entry point {enabled_name} did not return a ToolPack")
        pack = cast(ToolPack, candidate)
        if pack.name != enabled_name:
            raise ToolPackDiscoveryError(
                f"entry point {enabled_name} returned pack named {pack.name}"
            )
        packs.append(pack)
    return tuple(packs)


def _looks_like_pack(value: object) -> bool:
    return isinstance(getattr(value, "name", None), str) and callable(
        getattr(value, "create_tools", None)
    )
