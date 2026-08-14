"""Authoritative application tool-registry composition."""

from __future__ import annotations

from collections.abc import Sequence

from wyzer.desktop.system import WindowsSystemBackend
from wyzer.desktop.windows_backend import CtypesWindowsBackend
from wyzer.files import FileCatalog
from wyzer.runtime_paths import file_index_path
from wyzer.tools.builtin_packs import DEFAULT_CAPABILITY_PACKS, create_builtin_packs
from wyzer.tools.discovery import load_enabled_tool_packs
from wyzer.tools.files import FileToolPack
from wyzer.tools.packs import ToolPackFactory
from wyzer.tools.registry import ToolRegistry


def create_default_registry(
    backend: WindowsSystemBackend | None = None,
    *,
    audio_options: dict[str, object] | None = None,
    perception_options: dict[str, object] | None = None,
    enabled_entrypoint_packs: Sequence[str] = (),
    extra_pack_factories: Sequence[ToolPackFactory] = (),
    coding_agent_enabled: bool = True,
) -> ToolRegistry:
    """Build a fresh registry for the main process or an isolated worker.

    The built-in desktop capabilities are split into focused packs. Installed
    entry-point packs are never loaded automatically; each name must be
    explicitly enabled in configuration.
    """

    backend = backend or CtypesWindowsBackend(audio_options=audio_options)
    registry = ToolRegistry()
    for pack in create_builtin_packs(backend, perception_options):
        registry.register_pack(pack, default_visible=pack.name in DEFAULT_CAPABILITY_PACKS)
    registry.register_pack(
        FileToolPack(FileCatalog(file_index_path()), backend), default_visible=False
    )
    if coding_agent_enabled:
        from wyzer.coding.proxy import create_coding_agent_pack

        # Four compact proxies are the entire main-model coding surface. Keeping
        # them visible avoids a small model wrapping one self-contained coding
        # delegation in a redundant outer task plan before capability activation.
        registry.register_pack(create_coding_agent_pack(), default_visible=True)
    for pack_factory in extra_pack_factories:
        registry.register_pack(pack_factory(), default_visible=False)
    for pack in load_enabled_tool_packs(tuple(enabled_entrypoint_packs)):
        registry.register_pack(pack, default_visible=False)
    registry.finalize_capability_activation_surface()
    return registry
