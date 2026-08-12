# Changelog

All notable changes to Wyzer are documented here.

## [0.2.1] - 2026-08-12

### Added

- Add native `write_text_file`, `edit_text_file`, and `append_text_file` tools to the existing
  dynamically activated file capability, with compact model-facing schemas and structured results.
- Add exact-occurrence edits, optional SHA-256 preconditions, explicit file and parent creation,
  Unicode coverage, and session-context tracking for successfully mutated files.

### Security

- Require explicit overwrite intent and existing conditional confirmation before replacing a text
  file, while rejecting protected, sensitive, invalid, symbolic-link, and binary targets.
- Stage replacements beside the target, recheck the original bytes for concurrent changes, and use
  atomic replacement so failures do not partially modify existing files.

## [0.2.0] - 2026-08-12

### Added

- Add bounded, session-only task continuity for recently observed windows, applications, files,
  projects, folders, managed-browser pages and tabs, monitors, actions, and entities.
- Add compact model-facing session snapshots derived from authoritative successful tool results,
  plus debug summaries and debug-level context logging for developers.
- Add continuity coverage for current and previous windows, monitor moves, file search/open flows,
  browser tabs, failed and unverified actions, history bounds, and ordered multi-tool sequences.

### Changed

- Let the primary LLM resolve references such as “it,” “the previous window,” and “the first one”
  semantically from structured session facts and conversation history, then author concrete tool
  arguments through the existing guarded tool path.
- Avoid duplicating broad recent application, window, file, and website lists in production model
  context while retaining bounded native conversation and compact tool-result messages.

### Removed

- Remove deterministic generic-window argument rewriting and the unused regex reference resolver.

## [0.1.10] - 2026-08-10

### Fixed

- Keep task-plan validation details in internal diagnostics instead of exposing schema errors or
  asking users to supply planning fields for simple requests.
- Guide installed-game count and listing requests to the existing direct inventory tool, including
  count-only replies that omit game names.

### Changed

- Clarify that capability activation must be followed by the requested direct action and does not
  make otherwise simple work require a persistent task plan.
- Hide file-index maintenance from the model-facing file capability while preserving its internal
  registration, and strengthen named-folder/project semantics without deterministic text routing.
- Add first-decision and bounded end-to-end regression cases for game inventory and named-project
  opening, retaining strict failure behavior when a requested project is not actually opened.

## [0.1.9] - 2026-08-10

### Added

- Generate compact capability-specific activation tools from registered pack metadata while
  keeping capability selection entirely LLM-driven and activation effective on the next provider
  round only.
- Add bounded end-to-end model acceptance trajectories with controlled tool results, explicit
  outcome and safety rules, and efficiency scoring separate from first-decision quality.

### Changed

- Allow small compound requests to execute as sequential native tool calls without creating a
  persistent task plan; complex dependency-aware work continues to use the persistent task engine.
- Stop a returned direct-call sequence after failure or cancellation, preserve structured evidence
  for each call, and hold later calls behind exact confirmation boundaries.
- Clarify managed-browser versus ordinary personal Chrome semantics in the model-facing capability
  surface without adding deterministic intent routing.

## [0.1.8] - 2026-08-10

### Fixed

- Keep LLM-authored task planning and step-update calls on a separate bounded coordination budget,
  so completed multi-action work is not reported as a tool-round loop.
- Prevent a later mutating action from attaching its evidence to a step that is still awaiting a
  read-only verification observation.
- Clarify in the model-facing media schema that “skip” means the next track, not the previous one.

## [0.1.7] - 2026-08-10

### Fixed

- Serialize desktop UI requests so rapid text or voice submissions cannot interleave status and
  replies, and suppress duplicate stop acknowledgements after an interrupted request unwinds.
- Load OpenWakeWord before Qt on Windows so UI voice mode does not fail from conflicting native
  DLL load order; align the installer readiness check with the working import order.
- Treat custom avatar frames as optional in the readiness check, matching the built-in mascot
  fallback documented for the desktop UI.
- Close Windows 11 Calculator reliably when its direct UWP window ignores normal close messages,
  using an exact Calculator-process fallback only after normal verification fails.
- Make managed-browser tab inspection read-only when the browser is stopped instead of launching a
  new Chrome instance, and preserve managed-versus-personal Chrome scope in results.
- Require an explicitly scoped confirmation before closing a personal Chrome window.
- Respect personalized assistant names in desktop tray and character actions.
- Label completed or cancelled persisted plans as the last task instead of the current task.

### Changed

- Expand the live model acceptance documentation to the current 27 representative decisions.
- Tighten model guidance around Chrome scope and concise routine replies.

## [0.1.6] - 2026-08-09

### Added

- Add `python -m wyzer.dev.measure_context` for reproducible system-prompt and native-tool
  schema measurements, including per-tool and per-pack breakdowns.
- Add semantic schema regression coverage for tool names, arguments, JSON types, required
  fields, enum values, defaults, and validation constraints.

### Changed

- Reduce empty-state model-facing context from approximately 35,168 to 24,778 characters
  (29.5%) by compressing duplicated prompt/schema prose and omitting display-only schema titles.
- Preserve all 48 model-visible capability tools, three task-engine tools, native structured
  calling, validation, safety, perception, desktop typing, diagnostics, and LLM-driven routing.

## [0.1.5] - 2026-08-09

### Fixed

- Keep recovered paused or blocked tasks out of unrelated new conversations after restart.
- Restore saved task context only when the user explicitly resumes the task.

## [0.1.4] - 2026-08-09

### Fixed

- Exclude Wyzer's desktop avatar and chat process from normal window inventory and control
  actions, including when tools run in an isolated child process.
- Exclude Windows shell infrastructure such as Program Manager, Search, and desktop-host
  processes while preserving normal File Explorer windows.

## [0.1.3] - 2026-08-09

### Fixed

- Route explicit personal or current Chrome close requests to the normal desktop window while
  reserving managed-browser shutdown for Wyzer's automation profile.
- Ask which Chrome instance to close when a request is ambiguous and has no useful recent context.
- Exclude Wyzer's managed Chrome or Edge process tree from normal desktop window actions.

## [0.1.2] - 2026-08-09

### Fixed

- Detect NVIDIA GPUs during Windows installation and install the official CUDA 12.1 PyTorch wheel
  instead of leaving Kokoro TTS on the CPU-only PyTorch build.
- Report the installed PyTorch version, CUDA runtime, availability, and device count in the
  installer readiness check.
- Add `-TorchDevice auto|cuda|cpu` installer control for automatic detection or an explicit build.

## [0.1.1] - 2026-08-09

### Added

- Read-only 25-case acceptance suite for evaluating the configured model against Wyzer's real
  native tool schemas without executing desktop actions.

### Fixed

- Prevent later actions from being recorded as evidence for an already-verified task step.
- Stop offering tools after every planned step is verified, including when a local model emits an
  unsupported post-completion call.
- Remove completed and cancelled plans from subsequent model context.
- Clarify model routing for background processes, desktop-window checks, visual clicks, managed
  browser actions, and multi-action task planning.
- Restore automatic CPU/GPU selection for text-to-speech in the portable default configuration.

## [0.1.0] - 2026-08-09

### Added

- First public Windows installer release.
- Private per-user installer environment and desktop launcher.
- Custom avatar and wake-word asset packaging.
- Guided installer README with Python, Ollama, and model setup instructions.
- Repository contribution, security, and community documentation.
