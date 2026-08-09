# Architecture

Wyzer is LLM-first for ordinary conversation and desktop requests. Deterministic routing is reserved
for local controls such as interruption, confirmation answers, and explicit memory commands.

```text
                         no tool calls
user -> chat provider -----------------> assistant reply
           |
           | native tool_calls (capability or silent task-state operation)
           v
     registry lookup -> Pydantic validation -> confirmation policy -> executor/worker
           ^                                                        |
           |________________ compact role=tool result ______________|
                                |
                                v
                         final chat reply
```

## Request lifecycle

1. The conversation manager records the user message and bounded recent context.
2. The system prompt and native function definitions are sent in one provider chat request.
3. A normal assistant message ends the turn immediately.
4. An assistant message with `tool_calls` is preserved in history.
5. Calls are resolved only through `ToolRegistry`; arguments are validated with the tool's Pydantic
   model before execution. Calls run sequentially in returned order.
6. Each deterministic result is compacted, recorded normally with full evidence, and appended as a
   named `role=tool` message.
7. The same chat continues until the model returns final text, confirmation is required, the user
   interrupts, a provider fails, or the configured round limit is reached.

Every user action has a UUID. Every tool execution has a separate step UUID, preserving the event
and worker interfaces. Empty model output gets one corrective retry. Tool loops are bounded by
configuration and never continue without a limit.

For requests requiring multiple distinct computer actions, the same chat model can call the
orchestrator-owned `task_plan_create`, `task_step_update`, and `task_plan_revise` functions. These
are state operations, not a second planner model or deterministic intent router. The orchestrator
attaches real tool results to the current step and rejects a verified transition unless qualifying
evidence exists. An active plan also prevents unsupported final completion text.

## Package responsibilities

- `app`: native tool loop, compact tool context, and text/voice interfaces.
- `brain`: typed chat provider boundary, Ollama/OpenAI-compatible adapters, and system prompt.
- `conversation`: bounded transcript, native message history, references, and recent entities.
- `desktop`: Windows application index plus Win32 process, window, monitor, audio, and media backends.
- `perception`: on-demand Windows screenshot capture and the local Ollama vision client.
- `events`: bounded structured event ledger.
- `memory`: explicit-consent local facts.
- `policy`: exact-call confirmation policy.
- `state`: main-process deterministic world state.
- `tasks`: persistent LLM-authored plans, step transitions, retry bounds, and evidence gates.
- `tools`: typed contracts, eleven focused built-in packs, optional entry-point discovery, and the authoritative registry. Browser, clipboard, desktop UI interaction, screen perception, and bounded diagnostics live here as core capabilities.
- `examples/wyzer_example_pack`: intentionally non-functional reference structure for third-party pack authors.
- `workers`: in-process tests and isolated production execution.

The model never mutates Windows directly. It can only request registered functions. Tool evidence,
not model prose, establishes what occurred.

## Shared desktop scene

The main-process world state merges successful observations into a compact `desktop_scene`. Windows
queries contribute foreground/window state, managed-browser tools contribute page/tab state, and
vision/UI Automation contribute visible text, controls, and dialogs. Each source carries an
observation time, freshness window, and confidence. Browser page content is discarded after
navigation so the model cannot mistake it for current evidence.

The scene is on-demand: it does not capture screenshots or poll applications. It is context for the
LLM, not a deterministic action planner. The model must obtain fresh tool references before acting;
the scene intentionally contains no screen coordinates, UIA references, or browser element refs.

## Perception path

Screen perception is intentionally on demand rather than attached to every chat request.

```text
visual question -> inspect_screen -> screenshot -> same local Ollama/Qwen vision model
                                      -> compact visual understanding

visible click -> activate_visual_target -> screenshot -> vision target location -> Windows click
                                            |
                                            +-> low confidence / vision failure
                                                -> internal UIA lookup/click fallback

keyboard input -> type_desktop_text / press_desktop_key
```

The main chat model never receives raw HWNDs, UIA element references, or screen coordinates. Vision
is the primary perception/target-selection path. `inspect_desktop_ui` and `click_desktop_element` are
still registered for internal use but are hidden from the model and used only as a fallback. Visual
locations below the configured confidence threshold are never guessed; Wyzer either uses the UIA
fallback or returns a structured failure. Consequential targets still pass through confirmation.
