# Changelog

All notable changes to Wyzer are documented here.

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
