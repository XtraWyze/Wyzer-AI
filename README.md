# Wyzer

[![CI](https://github.com/XtraWyze/Wyzer-Ai/actions/workflows/ci.yml/badge.svg)](https://github.com/XtraWyze/Wyzer-Ai/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> A local Windows AI desktop assistant with voice control, a custom animated character, wake-word
> support, text-to-speech, speech recognition, and Ollama-powered local AI.

## Download

For the simplest Windows installation, download the latest `Wyzer-Setup.zip` from
[Releases](https://github.com/XtraWyze/Wyzer-Ai/releases/latest), extract it, and follow its
`README.txt`. The installer uses a private Python environment and does not modify global Python
packages.

Wyzer is a local, LLM-first Windows desktop assistant. It uses native model tool calling for
desktop actions while keeping every effect behind a typed, registered, deterministic tool.

```text
user -> one chat request -> answer
                         \-> native tool call -> validated tool -> tool result -> final chat reply
```

Ordinary conversation takes one model request. A simple desktop action normally takes one request
that selects a tool, local execution, and one follow-up request containing the compact result.
There are no routing or intent-extraction model calls. For genuinely multi-step computer work, the
same chat model may create and revise a silent task plan through native planning functions; simple
actions and conversation keep the direct path.

For planned work, every step carries explicit success criteria and tool evidence. Wyzer refuses to
claim the task is complete while a step remains unverified. Type or say `task status`, `pause`,
`resume`, or `stop` to control longer work without turning ordinary requests into command syntax.

## Development setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[audio,dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

Configure a tool-capable local model in `wyzer.toml`, start Ollama, and run:

```powershell
python -m wyzer
```

Browser, clipboard, desktop interaction, and local screen perception are installed with Wyzer
itself. Focus-sensitive keyboard and clipboard actions verify the expected target window immediately
before sending input. To inspect all registered capability packs, run:

```powershell
python -m wyzer --list-tool-packs
```

Try `How are you?`, `Open Calculator`, `Move the current window to the monitor on the right`,
`Search the web for local LLM tool calling`, or `Copy the highlighted text`. Say or type `stop` or `cancel` to interrupt.

## Safety boundary

`ToolRegistry` is the only model-visible capability list. Native function definitions are generated
from each registered tool's Pydantic argument model, so there is no duplicate tool manifest. Unknown
tools and invalid arguments become structured tool errors and are never imported or executed.

Routine reversible actions run immediately. Consequential actions pause at the final boundary and
ask a natural yes/no question. Approval is bound internally to the exact validated tool name and
arguments, expires, and never requires a visible token.

Wyzer retains its isolated workers, timeouts, cancellation, event ledger, conversation, persistent
task plans and memory state, application index, Win32 window/process/audio backends, file tools, voice recognition, wake
word, speech output, and Ollama diagnostics/auto-start behavior. Model-safe desktop UI Automation
is provided by the built-in `desktop_interaction` pack. The built-in `perception` pack captures
screenshots only on demand and sends them to the configured local Ollama vision model; raw screen
coordinates are never returned to the main model. Browser work remains isolated in the built-in
managed-browser pack.

An on-demand shared desktop scene combines live Windows state, managed-browser inspection, local
vision, and UI Automation. It records freshness and recent changes, removes sensitive visible text
before context use, and never contains raw coordinates or reusable control IDs.

Windows Core Audio support is optional: install `.[audio]` for exact master volume and independent
per-application session control. Without it, simple relative master controls can use an honest
media-key fallback; exact levels and per-application changes return a structured unavailable error.

See [Architecture](docs/ARCHITECTURE.md), [Task engine](docs/TASK_ENGINE.md), [Tools](docs/TOOLS.md), [Safety](docs/SAFETY.md),
[Local LLM setup](docs/LOCAL_LLM_SETUP.md), and [Speech](docs/SPEECH.md).

## Tool packs

The default registry is split into eleven focused built-in packs: `applications`, `audio`, `browser`,
`clipboard`, `desktop_interaction`, `diagnostics`, `files`, `media`, `perception`, `system`, and
`windows`. It registers 56 tools. The `diagnostics` pack exposes one bounded, read-only
`diagnose_system` tool instead of flooding the local model with low-level telemetry schemas.
The external entry-point system remains available only for optional third-party extensions. See
`docs/TOOL_PACKS.md`.

## Browser control

The built-in browser pack controls a dedicated managed Chrome profile through Chromium CDP. Web
searches and URL navigation start Chrome automatically. Page inspection returns stable short-lived
element refs for click and typing actions, avoiding UI Automation and blind coordinates.

## Screen perception

`inspect_screen` uses the same configured local Ollama model for on-demand vision. Normal turns do
not include screenshots. Vision is the primary desktop perception path. `activate_visual_target`
locates a human-described target visually and clicks it without exposing coordinates to the main
model. If vision is unavailable or too uncertain, Wyzer may try Windows UI Automation internally as
a conservative fallback. The UIA inspect/click plumbing is hidden from the model.

## Optional desktop character UI

Wyzer can run as a lightweight desktop companion without changing its LLM-first tool-calling
architecture. Install the optional Qt dependency and launch the same assistant with the character UI:

```powershell
pip install -e ".[ui]"
python -m wyzer --ui --voice
```

Use `python -m wyzer --ui --text` for the character plus text chat without wake-word listening.
The character can be dragged, double-clicked to open chat, and right-clicked for Wyzer controls.
It also shows short speech bubbles for listening/thinking/replies and occasional optional ambient comments.

The built-in character is a simple vector placeholder. To use custom artwork, place transparent PNG
or WebP frames named `idle1`, `walk1`, `drag1`, `fall1`, or `sit1` (plus numbered frames) in
`.wyzer/avatar/`. Wyzer loads available behavior frames on the next launch and falls back to idle
art for missing behaviors.
See [Desktop companion UI](docs/DESKTOP_UI.md) for the interaction and architecture details.

## Install on another Windows PC

Wyzer includes a Windows bootstrap installer that requires 64-bit Python 3.11 and creates its own
private virtual environment. It installs the tested speech/UI dependencies, copies the custom
avatar and wake-word assets, downloads the configured Faster-Whisper model, and creates a desktop
shortcut without changing the PC's global Python packages.

Build the transferable ZIP on the working Wyzer PC:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build-release.ps1
```

Copy `dist\Wyzer-Setup.zip` to the destination PC, extract it, and run `install.ps1`. Installed
configuration, avatars, models, memory, and task state live under `%LOCALAPPDATA%\Wyzer` and are
preserved by later reinstalls. See [the installer guide](installer/README.md) for details.
