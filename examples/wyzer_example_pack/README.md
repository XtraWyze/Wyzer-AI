# Non-functional external tool-pack example

This directory is **documentation only**. It is intentionally not installable as a Wyzer-discoverable
pack: its `pyproject.toml` has no `wyzer.tool_packs` entry point, and the example tool is marked
unavailable and raises if called directly.

Use it to see the basic package, argument/result model, handler, and `SimpleToolPack` shape. When
building a real third-party pack, copy the structure into a separate project, implement a real
handler, and add the entry point described in `docs/TOOL_PACKS.md`.
