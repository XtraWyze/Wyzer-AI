# Tool system

`ToolRegistry` is Wyzer's sole model-visible allowlist. Every tool declares a unique name,
description, Pydantic argument/result types, read-only status, confirmation policy, availability,
timeout, and capability-pack ownership. Native definitions are always generated from this registry.

The default `ModelToolView` contains the `applications`, `audio`, `media`, `system`, and `windows`
packs plus two visibility coordination tools. Specialized packs are absent until the same primary
LLM requests compact discovery and activates one. Activation can expose only already registered,
available, `llm_visible` tools; it cannot change argument validation, confirmation policy, risk,
execution, or evidence handling. It is capability visibility filtering, not intent routing.

`list_tool_capabilities` returns only optional pack names, visible tool counts, and active status.
`activate_tool_capability` retains one pack for the current action and, when a task plan is active,
in that plan. The activated tools are offered on the next provider round. Unrelated packs do not
accumulate across completed actions, and hidden/internal tools are never surfaced.

## Capability packs

### Applications

- `open_application`: focus an existing desktop app or launch it when absent.
- `search_installed_applications`
- `list_installed_applications` (registered maintenance/read tool; hidden from the model)
- `refresh_application_index` (registered maintenance tool; hidden from the model)
- `list_installed_games`
- `open_file`

### Windows

- `get_foreground_window`
- `list_open_windows`
- `control_named_window`: focus, minimize, maximize, restore, or close by app/title.
- `move_named_window_to_monitor`
- `get_monitor_layout`

These tools use stable application/window identities. Raw HWND action tools are intentionally not
model-visible.

### Browser

The browser pack is activated on demand.

The built-in `browser` pack exclusively owns webpage actions:

- `browser_start` and `browser_status` (registered but hidden from the model)
- `browser_stop`
- `browser_open_url`
- `browser_search_web`
- `browser_inspect_page`
- `browser_click`
- `browser_type_text`
- `browser_press_key`
- `browser_scroll`
- `browser_history`
- `browser_list_tabs`
- `browser_switch_tab`
- `browser_close_tab`

Managed Chrome starts automatically for ordinary browser actions. Page inspection returns
short-lived element references used by click/type, keeping web interaction separate from desktop UI
Automation and blind coordinates.

### Clipboard

The on-demand built-in `clipboard` pack provides:

- `read_clipboard`
- `write_clipboard`
- `copy_selected_text`
- `paste_clipboard`

Selected-text copy and paste require the expected `target_window` and fail closed if focus changes
before the shortcut is sent.

### Desktop interaction

The on-demand built-in `desktop_interaction` pack retains Windows UI Automation, but UIA is no longer the
model's primary desktop perception path:

- `inspect_desktop_ui` — internal/hidden fallback
- `click_desktop_element` — internal/hidden fallback
- `type_desktop_text` — model-visible keyboard input
- `press_desktop_key` — model-visible keyboard input

The hidden UIA tools return opaque element IDs instead of screen coordinates or raw HWNDs. The
vision-first perception tools call them internally only when vision fails or cannot identify a target
reliably. Keyboard tools require `target_window`, re-check the focused title/application immediately
before input, and reject the action if focus changed. Multi-tab applications still require the
intended tab or control to be visibly activated first.

Webpage tasks should remain on the `browser_*` tools rather than using desktop interaction against Chrome.


### Screen perception

The on-demand built-in `perception` pack adds two high-level tools:

- `inspect_screen`: capture the focused window or full desktop and ask the configured local Qwen/Ollama vision model what is visibly present.
- `activate_visual_target`: primary visible-target click path. It uses Qwen vision first and may fall back internally to UI Automation.

Screenshots and raw coordinates stay inside the perception worker. The main model receives only a
compact visual summary, visible text, useful element descriptions, and action evidence. Visual clicks
use a minimum confidence threshold; if vision is uncertain, Wyzer tries the hidden UIA fallback rather
than guessing.

### Diagnostics, system, audio, media, and files

The built-in `diagnostics` pack exposes one model-visible read-only tool:

- `diagnose_system`: gather a bounded Windows health snapshot with scopes `auto`, `performance`,
  `hardware`, `storage`, `network`, `windows`, and `security`. It can include CPU/RAM load, top
  processes, disk/network I/O, vendor-neutral GPU telemetry, volume and physical-disk health,
  network adapters/connectivity, battery/firmware data, stopped automatic services, device error
  codes, recent serious System events, pending reboot state, Defender, and firewall status.
  NVIDIA cards use `nvidia-smi` when available for utilization, VRAM, temperature, and power.
  AMD Radeon cards use Windows GPU performance counters plus CIM/driver metadata for utilization,
  dedicated VRAM use, adapter memory where Windows exposes it, and driver information without
  requiring ROCm or an extra Python package. Unsupported vendor-specific sensor fields are returned
  as unavailable rather than causing the diagnostic to fail.

The low-level collection remains inside the Windows backend rather than becoming dozens of model
tools. Results are bounded and classified into informational, attention, and warning findings so the
LLM can reason over current evidence without directly changing the machine. Network scope performs
a small connectivity probe; all other diagnostic collection is local and read-only.

System tools inspect the computer and processes. Audio tools control the master output and active
application sessions. Media tools use Windows media controls.

The built-in `files` pack provides discovery plus direct file management:

- `search_files` and `read_text_file`
- `list_directory`
- `create_directory`
- `copy_path`
- `move_path`
- `rename_path`
- `delete_path` — sends the target to the Windows Recycle Bin and always requires confirmation
- `open_indexed_folder`
- `refresh_file_index` - a quick, bounded metadata-only scan of common user folders
- `deep_scan_file_index` - a full local-drive scan with optional bounded text indexing

Wyzer runs the same quick scan in the background at startup. It checks at most 20,000 files for up
to five seconds and does not read file contents, so the UI and voice/model initialization remain
responsive. Both maintenance tools are model-visible after the `files` capability is activated.
The deep scan checks every local drive, can take several minutes, and always presents a dedicated
confirmation before it starts. Incomplete or erroring scans update observed files without deleting
unvisited catalog entries.

Copy, move, and rename never overwrite an existing destination implicitly. Destructive changes to
drive roots and protected Windows/program directories are rejected. File management results are
added to Wyzer's recent-file context so follow-ups such as "rename it" can resolve naturally.

## Windows audio mixer

`control_master_audio` controls only the default output's master level. Its operations are
`increase`, `decrease`, `set`, `mute`, `unmute`, `toggle_mute`, and `get`.

`control_application_audio` applies the same operations to a named application's active Windows
audio sessions. Multiple sessions for the same application are changed together by default;
`scope = "one"` targets one deterministic match.

Install exact Core Audio support with:

```powershell
python -m pip install -e ".[audio,dev]"
```
