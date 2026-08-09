# Changelog

All notable changes to Wyzer are documented here.

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
