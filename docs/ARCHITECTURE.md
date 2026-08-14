# Architecture

Wyzer is LLM-first for ordinary conversation and desktop requests. Deterministic routing is reserved
for local controls such as interruption, confirmation answers, and explicit memory commands.

```text
                         no tool calls
user -> chat provider -----------------> assistant reply
           |
           | native tool_calls (action, capability visibility, or task state)
           v
     registry model view -> registry lookup -> Pydantic validation -> confirmation -> worker
           ^                                                        |
           |________________ compact role=tool result ______________|
                                |
                                v
                         final chat reply
```

## Request lifecycle

1. The conversation manager records the user message and bounded recent context.
2. The system prompt and a capability-scoped registry view are sent in one provider chat request.
3. A normal assistant message ends the turn immediately.
4. An assistant message with `tool_calls` is preserved in history.
5. Calls are resolved only through `ToolRegistry`; arguments are validated with the tool's Pydantic
   model before execution. Calls run sequentially in returned order.
6. Each deterministic result is compacted, recorded normally with full evidence, and appended as a
   named `role=tool` message.
7. Successful observed results also update the bounded in-memory session context before the next
   model round. Failed or explicitly unverified actions do not advance entity state.
8. The same chat continues until the model returns final text, confirmation is required, the user
   interrupts, a provider fails, or the configured round limit is reached.

Every user action has a UUID. Every tool execution has a separate step UUID, preserving the event
and worker interfaces. Empty model output gets one corrective retry. Tool loops are bounded by
configuration and never continue without a limit.

`ToolRegistry` remains the only capability source. Its immutable `ModelToolView` projects a small
default set plus capability packs activated for the current action. The same primary LLM can call
`list_tool_capabilities` and `activate_tool_capability` when specialized tools are absent; the
new schemas appear on the next native round. These calls change visibility only. They do not infer
intent, import code, execute an action, or count as evidence. Registered hidden tools remain hidden
in every view.

Applications, windows, audio, media, and lightweight system inspection stay in the default view so
routine requests do not pay a discovery round. The four coding-agent coordination proxies also stay
in the default view so a small model can delegate self-contained software work directly. Browser,
clipboard, desktop interaction, diagnostics, files, perception, and enabled third-party packs are
activated on demand. There is no
keyword/regex capability router and no second planner model.

The `coding_agent` pack is a persistent coordination surface. Its four proxy definitions remain
registry-owned and pass ordinary visibility and Pydantic validation, but `Orchestrator` executes
them through a main-process `CodingAgentManager` instead of a disposable worker. The manager reuses
the exact primary `ChatProvider` object with a coding-specific prompt, separate bounded message
histories, workspace-scoped tools, and its own bounded native-tool loop. Only a compact session
summary and structured result return to the main conversation. See
[`CODING_AGENT.md`](CODING_AGENT.md).

For longer work with meaningful dependencies, intermediate artifacts, retries, recovery, or
cross-step verification, the same chat model can call the orchestrator-owned `task_plan_create`,
`task_step_update`, and `task_plan_revise` functions. Small immediately executable sequences can
instead return several ordinary native calls, which execute sequentially through the same guarded
tool path. Each direct call retains registry validation, confirmation, isolation, cancellation, and
separate evidence. A failure stops the remaining returned calls for model reassessment, while a
confirmation boundary preserves but does not execute the tail until confirmation succeeds. These
are model decisions, not a second planner model or deterministic intent router. For planned work,
the orchestrator attaches real tool results to the current step and rejects a verified transition
unless qualifying evidence exists. An active plan also prevents unsupported final completion text.

## Package responsibilities

- `app`: native tool loop, compact tool context, and text/voice interfaces.
- `brain`: typed chat provider boundary, Ollama/OpenAI-compatible adapters, and system prompt.
- `conversation`: bounded transcript, native message history, and session-only continuity facts.
- `coding`: retained coding sessions, coding prompt/loop, workspace containment, and coding-only tools.
- `desktop`: Windows application index plus Win32 process, window, monitor, audio, and media backends.
- `perception`: on-demand Windows screenshot capture and the local Ollama vision client.
- `events`: bounded structured event ledger.
- `memory`: explicit-consent local facts.
- `policy`: exact-call confirmation policy.
- `state`: main-process deterministic world state.
- `tasks`: persistent LLM-authored plans, step transitions, retry bounds, and evidence gates.
- `tools`: typed contracts, twelve focused built-in packs, capability-scoped model views, optional entry-point discovery, and the authoritative registry.
- `examples/wyzer_example_pack`: intentionally non-functional reference structure for third-party pack authors.
- `workers`: in-process tests and isolated production execution.

The model never mutates Windows directly. It can only request registered functions. Tool evidence,
not model prose, establishes what occurred.

## Session context and task continuity

`SessionContextManager` is a small process-local fact tracker. Every completed tool call reaches it
through the orchestrator's existing post-result seam, after world-state observation has been applied.
It tracks the active and previous window/application, current folder/project, last and recent files,
managed-browser page/tab, last and previous user-facing monitor, compact last-call/result metadata,
and bounded recent action/entity histories. Window handles, paths, URLs, tab indexes, and monitor
metadata come from typed tool results; the tracker does not manufacture identifiers.

Before each provider call, the orchestrator adds a compact `session_context` object to the existing
`CONTEXT_JSON`. Ordinary model-message history remains intact, while the older broad recent-app,
window, file, and website lists are omitted from production prompt assembly to avoid duplicating the
same grounding. Raw file contents, browser DOMs, UI trees, screenshots, and full tool payloads never
enter the session snapshot. Histories default to eight items and the model-facing representation has
an independent 2,400-character ceiling.

Reference resolution remains LLM-driven. Python never scans user text for pronouns or rewrites a
window target. The prompt tells the model to interpret ordered session facts and author a concrete
tool argument; ambiguity is handled by asking the user. The normal registry, validation,
confirmation, sequential multi-call, failure-stop, and evidence paths are unchanged. Context updates
after every completed call in a returned sequence, so the next model round sees the newest observed
state.

Developers can inspect `orchestrator.session_context.snapshot()` or the one-line
`orchestrator.session_context.debug_summary()`. Each update also emits a debug-level
`[session-context]` log entry; normal user output is unaffected.

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
