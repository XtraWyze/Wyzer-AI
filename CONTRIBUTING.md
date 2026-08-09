# Contributing to Wyzer

Thanks for helping improve Wyzer. Small, focused changes are easiest to review and safest to test.

## Before you start

- Search existing issues before opening a new one.
- For larger changes, open an issue first so the intended behavior can be discussed.
- Do not submit API keys, personal data, local memory databases, downloaded models, or private avatar art
  unless you own it and explicitly want it distributed.

## Development setup

Wyzer supports 64-bit Python 3.11 on Windows.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[audio,ui,dev]"
python -m pytest
python -m ruff check .
python -m mypy
```

## Pull requests

- Keep the change narrowly scoped.
- Add or update tests for behavioral changes.
- Update user documentation when setup, configuration, or visible behavior changes.
- Run the checks above before requesting review.
- Explain what changed, why it changed, and how you tested it.

By contributing, you agree that your contribution may be distributed under the [MIT License](LICENSE).
