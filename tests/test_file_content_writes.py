from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.fake_windows import FakeWindowsBackend
from wyzer.conversation import SessionContextManager
from wyzer.files import FileCatalog
from wyzer.models import ConfirmationMode
from wyzer.policy import ConfirmationPolicy
from wyzer.tools import create_default_registry
from wyzer.tools.base import ToolContext, ToolExecutionError
from wyzer.tools.files import (
    AppendTextFileArguments,
    AppendTextFileTool,
    EditTextFileArguments,
    EditTextFileTool,
    WriteTextFileArguments,
    WriteTextFileTool,
)
from wyzer.workers import InProcessExecutor


def context() -> ToolContext:
    return ToolContext(action_id=uuid4(), step_id=uuid4())


def catalog(tmp_path: Path) -> FileCatalog:
    return FileCatalog(tmp_path / "index.sqlite3")


def test_write_creates_utf8_text_and_empty_files(tmp_path: Path) -> None:
    tool = WriteTextFileTool(catalog(tmp_path))
    unicode_path = tmp_path / "nested" / "unicode.txt"

    result = tool.execute(
        WriteTextFileArguments(
            path=unicode_path,
            content="Hello, 世界 👋",
            create_parents=True,
        ),
        context(),
    )
    empty = tool.execute(
        WriteTextFileArguments(path=tmp_path / "empty.txt", content=""), context()
    )

    assert unicode_path.read_bytes() == "Hello, 世界 👋".encode()
    assert result.path == str(unicode_path.resolve())
    assert result.created is True
    assert result.kind == "file"
    assert empty.created is True
    assert (tmp_path / "empty.txt").read_bytes() == b""


def test_write_never_silently_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("keep", encoding="utf-8")
    tool = WriteTextFileTool(catalog(tmp_path))

    with pytest.raises(ToolExecutionError) as failure:
        tool.execute(WriteTextFileArguments(path=path, content="replace"), context())

    assert failure.value.code == "FILE_EXISTS"
    assert path.read_text(encoding="utf-8") == "keep"

    result = tool.execute(
        WriteTextFileArguments(path=path, content="replace", overwrite=True), context()
    )
    assert result.overwritten is True
    assert path.read_text(encoding="utf-8") == "replace"


def test_write_overwrite_uses_conditional_confirmation(tmp_path: Path) -> None:
    definition = WriteTextFileTool(catalog(tmp_path)).definition()
    policy = ConfirmationPolicy()

    assert definition.confirmation is ConfirmationMode.CONDITIONAL
    assert policy.requires_confirmation(definition, {"path": "new.txt", "overwrite": False}) is False
    assert policy.requires_confirmation(definition, {"path": "old.txt", "overwrite": True}) is True
    assert "replace the existing text file" in policy.issue(
        uuid4(), uuid4(), "write_text_file", {"path": "old.txt", "overwrite": True}
    ).prompt


def test_edit_replaces_only_the_expected_exact_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_text("timeout = 5\nlabel = 'timeout'\n", encoding="utf-8")

    result = EditTextFileTool(catalog(tmp_path)).execute(
        EditTextFileArguments(path=path, old_text="timeout = 5", new_text="timeout = 10"),
        context(),
    )

    assert path.read_text(encoding="utf-8") == "timeout = 10\nlabel = 'timeout'\n"
    assert result.path == str(path.resolve())
    assert result.occurrences_changed == 1
    assert result.summary == "Replaced 1 exact occurrence(s)."


@pytest.mark.parametrize(
    ("content", "old_text", "code"),
    [
        ("alpha\n", "missing", "TEXT_NOT_FOUND"),
        ("same\nsame\n", "same", "OCCURRENCE_COUNT_MISMATCH"),
    ],
)
def test_edit_count_failures_leave_file_unchanged(
    tmp_path: Path, content: str, old_text: str, code: str
) -> None:
    path = tmp_path / "notes.txt"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ToolExecutionError) as failure:
        EditTextFileTool(catalog(tmp_path)).execute(
            EditTextFileArguments(path=path, old_text=old_text, new_text="changed"), context()
        )

    assert failure.value.code == code
    assert path.read_text(encoding="utf-8") == content


def test_edit_allows_an_explicit_exact_multiple_count(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("same\nsame\n", encoding="utf-8")

    result = EditTextFileTool(catalog(tmp_path)).execute(
        EditTextFileArguments(
            path=path,
            old_text="same",
            new_text="changed",
            expected_occurrences=2,
        ),
        context(),
    )

    assert result.occurrences_changed == 2
    assert path.read_text(encoding="utf-8") == "changed\nchanged\n"


def test_edit_sha_precondition_detects_an_unexpected_change(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_text("timeout = 5\n", encoding="utf-8")
    stale_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("timeout = 5\n# changed elsewhere\n", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ToolExecutionError) as failure:
        EditTextFileTool(catalog(tmp_path)).execute(
            EditTextFileArguments(
                path=path,
                old_text="timeout = 5",
                new_text="timeout = 10",
                expected_sha256=stale_digest,
            ),
            context(),
        )

    assert failure.value.code == "FILE_CHANGED"
    assert path.read_bytes() == before


def test_edit_detects_change_during_atomic_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wyzer.tools import files

    path = tmp_path / "config.py"
    path.write_text("timeout = 5\n", encoding="utf-8")
    real_copymode = files.shutil.copymode

    def concurrent_change(source: Path, destination: Path) -> None:
        real_copymode(source, destination)
        source.write_text("changed elsewhere\n", encoding="utf-8")

    monkeypatch.setattr(files.shutil, "copymode", concurrent_change)
    with pytest.raises(ToolExecutionError) as failure:
        EditTextFileTool(catalog(tmp_path)).execute(
            EditTextFileArguments(path=path, old_text="timeout = 5", new_text="timeout = 10"),
            context(),
        )

    assert failure.value.code == "FILE_CHANGED"
    assert path.read_text(encoding="utf-8") == "changed elsewhere\n"
    assert list(tmp_path.glob(".config.py.*.tmp")) == []


def test_append_preserves_existing_content_and_can_explicitly_create(tmp_path: Path) -> None:
    existing = tmp_path / "notes.txt"
    existing.write_text("first\n", encoding="utf-8")
    tool = AppendTextFileTool(catalog(tmp_path))

    result = tool.execute(
        AppendTextFileArguments(path=existing, content="second 👋\n"), context()
    )
    created_path = tmp_path / "nested" / "new.txt"
    created = tool.execute(
        AppendTextFileArguments(
            path=created_path,
            content="new",
            create=True,
            create_parents=True,
        ),
        context(),
    )

    assert existing.read_text(encoding="utf-8") == "first\nsecond 👋\n"
    assert result.created is False
    assert created.created is True
    assert created_path.read_text(encoding="utf-8") == "new"


def test_append_missing_file_requires_explicit_create(tmp_path: Path) -> None:
    path = tmp_path / "missing.txt"

    with pytest.raises(ToolExecutionError) as failure:
        AppendTextFileTool(catalog(tmp_path)).execute(
            AppendTextFileArguments(path=path, content="text"), context()
        )

    assert failure.value.code == "PATH_NOT_FOUND"
    assert not path.exists()


def test_invalid_and_binary_paths_fail_without_modification(tmp_path: Path) -> None:
    tool = EditTextFileTool(catalog(tmp_path))
    binary = tmp_path / "image.bin"
    original = b"header\x00\x01payload"
    binary.write_bytes(original)

    with pytest.raises(ToolExecutionError) as binary_failure:
        tool.execute(
            EditTextFileArguments(path=binary, old_text="header", new_text="changed"), context()
        )
    with pytest.raises(ToolExecutionError) as path_failure:
        tool.execute(
            EditTextFileArguments(
                path=Path("invalid\x00name.txt"), old_text="a", new_text="b"
            ),
            context(),
        )

    assert binary_failure.value.code == "NOT_TEXT_FILE"
    assert binary.read_bytes() == original
    assert path_failure.value.code == "INVALID_PATH"


def test_sensitive_text_paths_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".env"

    with pytest.raises(ToolExecutionError) as failure:
        WriteTextFileTool(catalog(tmp_path)).execute(
            WriteTextFileArguments(path=path, content="TOKEN=secret"), context()
        )

    assert failure.value.code == "SENSITIVE_PATH"
    assert not path.exists()


def test_atomic_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wyzer.tools import files

    path = tmp_path / "notes.txt"
    path.write_text("before", encoding="utf-8")

    def deny_replace(source: object, destination: object) -> None:
        del source, destination
        raise PermissionError("write denied")

    monkeypatch.setattr(files.os, "replace", deny_replace)
    with pytest.raises(ToolExecutionError) as failure:
        EditTextFileTool(catalog(tmp_path)).execute(
            EditTextFileArguments(path=path, old_text="before", new_text="after"), context()
        )

    assert failure.value.code == "FILE_WRITE_FAILED"
    assert path.read_text(encoding="utf-8") == "before"
    assert list(tmp_path.glob(".notes.txt.*.tmp")) == []


def test_file_pack_activation_exposes_compact_content_write_schemas() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    default_names = set(registry.model_view().tool_names)
    active = {
        tool.function.name: tool.function.parameters
        for tool in registry.model_view(("files",)).native_tools()
    }

    assert {"write_text_file", "edit_text_file", "append_text_file"}.isdisjoint(default_names)
    assert {"write_text_file", "edit_text_file", "append_text_file"} <= set(active)
    assert set(active["write_text_file"]["properties"]) == {
        "path",
        "content",
        "overwrite",
        "create_parents",
    }
    assert set(active["edit_text_file"]["properties"]) == {
        "path",
        "old_text",
        "new_text",
        "expected_occurrences",
        "expected_sha256",
    }
    assert "CONTEXT_JSON user_folders" in active["write_text_file"]["properties"]["path"][
        "description"
    ]
    assert all(len(json.dumps(active[name])) < 1_500 for name in (
        "write_text_file",
        "edit_text_file",
        "append_text_file",
    ))
    activation = next(
        tool for tool in registry.native_tools() if tool.function.name == "activate_file_tools"
    )
    assert "write/edit/append text" in activation.function.description


def test_executor_result_path_flows_into_session_context(tmp_path: Path) -> None:
    registry = create_default_registry(FakeWindowsBackend())
    path = tmp_path / "notes.txt"
    result = asyncio.run(
        InProcessExecutor(registry).execute(
            "write_text_file",
            {"path": str(path), "content": "hello"},
            uuid4(),
            uuid4(),
        )
    )
    manager = SessionContextManager()
    manager.record_tool_result(result, {"path": str(path), "content": "hello"})

    assert result.ok is True
    assert result.data is not None and result.data["path"] == str(path.resolve())
    last_file = manager.snapshot().last_file
    assert last_file is not None
    assert last_file.path == str(path.resolve())
