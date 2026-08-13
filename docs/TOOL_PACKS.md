# Tool packs

Wyzer exposes tools through small capability packs. The model receives only registered tools, and
every call still passes Pydantic validation, confirmation policy, worker isolation, and typed
results.

## Built-in packs

The default registry contains twelve focused packs:

- `capabilities`: compact model-driven pack discovery and activation coordination.

- `applications`: open/focus apps, application discovery, games, and opening a local file.
- `audio`: master and per-application Windows audio controls.
- `browser`: managed Chrome/Edge webpage navigation, inspection, interaction, history, and tabs.
- `clipboard`: clipboard read/write plus focused-window copy and paste.
- `desktop_interaction`: inspect and interact with controls in the focused Windows desktop app.
- `diagnostics`: bounded read-only Windows telemetry and health diagnostics.
- `files`: indexed file search, bounded text reads, model-driven refresh, file management, and
  opening indexed folders.
- `media`: Windows play/pause, next, previous, stop, and current-media inspection.
- `perception`: local vision screen inspection and confidence-gated visual target activation.
- `system`: system profile, process inspection, and bounded waits.
- `windows`: named-window control and physical monitor movement.

The built-in registry contains 58 tools. Applications, audio, media, system, windows, and capability
coordination are in the default model view. Browser, clipboard, desktop interaction, diagnostics,
files, and perception remain built in but are activated on demand by the primary LLM. Built-in packs
do not need separate installation and must not be listed under `[tool_packs].enabled`.

## Migrating from the old optional packs

Older development copies installed browser, clipboard, and desktop interaction from `examples/`.
Those example packages have been removed because their code now lives under `wyzer/tools/`.

The shipped `wyzer.toml` should contain:

```toml
[tool_packs]
enabled = []
```

If you previously installed the old editable packages into the same virtual environment, they are
no longer used. You can remove them with:

```powershell
python -m pip uninstall wyzer-browser-pack wyzer-clipboard-pack wyzer-desktop-interaction-pack
```

Leaving them installed is harmless as long as they are not enabled as external packs.

The main Wyzer package now declares `playwright`, `pyperclip`, and `pywinauto` directly, so a normal
Wyzer install provides the Python dependencies for these built-ins. The browser tools drive an
installed Chrome or Edge through CDP; they do not require Playwright to download its own browser.

## External packs still exist

The entry-point extension system remains available for genuinely optional third-party capabilities.
Installed external packs never activate automatically. Only names explicitly listed in
`[tool_packs].enabled` are loaded.

Keep external packs small, clearly scoped, and user-oriented. Enabled external packs are registered
at startup but enter the model-visible view only after model-requested activation. Activation never
imports new code and cannot reveal a tool marked hidden.

## Documentation-only example

`examples/wyzer_example_pack` shows the shape of an external pack but is intentionally unusable:

- its `pyproject.toml` deliberately has no `wyzer.tool_packs` entry point;
- its example tool is marked unavailable; and
- its handler raises if somebody calls it directly.

This prevents the repository from quietly shipping another working capability while still leaving a
reference structure for developers.

A real external pack uses the same basic shape:

```python
from pydantic import BaseModel

from wyzer.models import RiskLevel, ToolArguments
from wyzer.tools import CallableTool, SimpleToolPack, ToolContext


class EchoArguments(ToolArguments):
    text: str


class EchoResult(BaseModel):
    text: str


def echo(arguments: EchoArguments, context: ToolContext) -> EchoResult:
    del context
    return EchoResult(text=arguments.text)


def create_pack() -> SimpleToolPack:
    return SimpleToolPack(
        "example",
        (
            lambda: CallableTool(
                name="echo_text",
                description="Return supplied text unchanged.",
                arguments_type=EchoArguments,
                result_type=EchoResult,
                handler=echo,
                risk_level=RiskLevel.LOW,
                read_only=True,
            ),
        ),
    )
```

For an installable real pack, register the factory in that package's `pyproject.toml`:

```toml
[project.entry-points."wyzer.tool_packs"]
example = "wyzer_example_pack:create_pack"
```

Module-level factories and handlers are important on Windows because isolated workers use the
`spawn` multiprocessing mode. If an enabled external pack is missing, malformed, duplicated, or
conflicts with an existing built-in tool/pack name, startup fails closed.
