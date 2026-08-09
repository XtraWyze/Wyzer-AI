# Development

Use Python 3.11 or later. Install development dependencies with `pip install -e ".[dev]"`.
Before completing a stage, run:

```powershell
pytest
ruff check .
ruff format --check .
mypy
```

Keep modules focused, dependencies directed toward shared models, and Windows APIs behind
interfaces. Unit tests use fakes; Windows integration tests must be explicitly selected and may
only operate safe applications such as Calculator and Notepad.

Never add a tool that silently succeeds. Test doubles belong in tests and must be unmistakable.

`FakeChatProvider` supplies deterministic assistant messages and native tool calls in tests. It is
not a fallback conversational model. With `provider = "none"`, Wyzer retains only explicit local
controls and memory commands and clearly reports that ordinary requests need a configured model.

Safe real-Windows integration tests are opt-in:

```powershell
$env:WYZER_RUN_WINDOWS_INTEGRATION = "1"
pytest -m windows_integration
```

They perform read-only inventory and briefly launch Notepad, then terminate only the exact process
created by the test. They never install software, modify security settings, enter credentials,
delete files, or communicate externally.
