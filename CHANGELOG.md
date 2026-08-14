# Changelog

All notable changes to Wyzer are documented here.

## [0.3.1] - 2026-08-14

### Added

- Add a dedicated, persistent coding-agent subsystem that reuses Wyzer's configured model while
  keeping a separate bounded conversation and workspace-scoped file, search, command, and Git tools.
- Add coding-agent configuration, documentation, model-acceptance cases, and end-to-end coverage for
  creating, continuing, running, testing, and debugging software projects.

### Changed

- Route software-development requests directly to the coding agent, preserve coding sessions across
  follow-ups, ground Desktop-relative workspaces, and reserve cancellation for explicit stop requests.

## [0.3.0] - 2026-08-14

### Fixed

- Check Ollama's installed-model list through its local API instead of running `ollama show` and
  treating the expected "model not found" result as a fatal PowerShell error.
- Pull the configured Ollama model in a child process whose exit code can be checked without native
  stderr bypassing the installer's friendly error handling.

## [0.2.11] - 2026-08-14

### Changed

- Turn Windows setup into a fresh-PC, one-double-click flow that installs a signed private Python
  runtime when needed, Ollama, the configured local AI model, speech dependencies, and shortcuts.
- Start Wyzer automatically after the final readiness check and add a Start Menu shortcut alongside
  the Desktop shortcut.
- Replace the manual Python, Ollama, and model prerequisites in the release guide with the automated
  setup flow.

### Fixed

- Refresh the installer process PATH after installing Ollama and fall back from WinGet to Ollama's
  signed official installer when WinGet is unavailable.
- Install Microsoft's signed Visual C++ x64 runtime when absent, satisfying ONNX Runtime's native
  Windows dependency on a genuinely fresh PC.
- Locate OpenWakeWord's bundled support-model directory without importing the entire speech stack
  before its native prerequisites and model assets are ready.
- Save a persistent installation transcript to `%LOCALAPPDATA%\Wyzer\install.log` and point failed
  double-click installs to it.
- Keep the package and runtime version identifiers synchronized.

## [0.2.10] - 2026-08-14

### Fixed

- Guide the LLM to create and edit text-based files through native file tools instead of opening
  Notepad or typing into another desktop editor unless the user explicitly requests that editor.
- Preserve requested line breaks in generated batch files, use a single `.bat` extension, and infer
  `.txt` when the user requests a text file without naming an extension.
- Add acceptance and end-to-end trajectory coverage that rejects app launching, file opening,
  desktop typing, and unnecessary planning for direct text and batch file creation.

## [0.2.9] - 2026-08-14

### Fixed

- Ground the LLM with the current Windows user's configured Desktop, Documents, Downloads,
  Pictures, Music, and Videos locations when it authors file paths.
- Describe file-tool paths as exact absolute paths so requests such as "put it on my desktop" use
  the user's real Desktop instead of creating a relative `Desktop` folder.

## [0.2.8] - 2026-08-13

### Fixed

- Make Windows setup verify the bundled custom wake models, force-copy them into both the standard
  and configured model directories, and confirm the installed files before the readiness check.
- Save every final installation diagnostic to `install-readiness.json` so a failed fresh install
  reports an actionable persistent path instead of referring only to console output.

## [0.2.7] - 2026-08-13

### Fixed

- Bundle and verify OpenWakeWord's required ONNX preprocessing models in the Windows setup ZIP so
  a fresh install does not depend on a separate GitHub model download.

## [0.2.6] - 2026-08-13

### Fixed

- Add a double-clickable installer launcher that uses a process-scoped PowerShell execution-policy
  bypass, allowing installation when local script execution is disabled without changing the PC's
  saved policy.
- Add a bounded metadata-only startup scan of common user folders, and reserve full
  local-drive/content indexing for a separately confirmed deep scan.
- Preserve catalog entries outside the requested scan roots and avoid pruning unvisited entries
  when a scan is interrupted, bounded, or encounters filesystem errors.

## [0.2.5] - 2026-08-13

### Fixed

- Keep the local file catalog on a stable per-user path after launching Wyzer through the elevated
  Windows shortcut, while preserving indexes created by earlier installed versions.
- Expose file-index refresh through the model-activated `files` capability so the primary LLM can
  select and execute maintenance from natural-language requests.

## [0.2.4] - 2026-08-13

### Changed

- Mark the installed Wyzer desktop shortcut to request administrator access through Windows UAC.

### Fixed

- Treat personal Chrome windows and Wyzer's managed browser as candidates for an unqualified Chrome
  close: close the sole candidate immediately, or ask which labeled candidate to close when several
  exist.

## [0.2.3] - 2026-08-13

### Changed

- Default Ollama text, tool, warm-up, and screen-perception requests to a configurable 32,768-token
  context window instead of relying on Ollama's smaller VRAM-based default.
- Apply the configured context length when Wyzer auto-starts the local Ollama server, while also
  sending `num_ctx` per request so the setting works with an already-running server.

## [0.2.2] - 2026-08-12

### Added

- Add a bounded runtime self-capability context generated from registered tool-pack metadata,
  current activation state, runtime availability, and orchestrator features without injecting
  hidden tool schemas.
- Add semantic model acceptance coverage for multi-action execution, authored intermediate steps,
  tool selection, result-driven continuation, autonomous-goal limits, and native text editing.

### Fixed

- Prevent capability questions from treating tool use or activation as evidence that an ability is
  missing, or from assuming that every multi-action request requires a persistent task plan.
- Distinguish ambiguous outcomes from missing capabilities, autonomous goal creation from planning
  a user-provided goal, and on-demand observation from continuous passive monitoring.
- Route “what can you do?” through the LLM and authoritative runtime capability context instead of
  the legacy static help response.

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
