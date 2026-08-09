from pathlib import Path
from uuid import uuid4

import pytest

from wyzer.files import FileCatalog
from wyzer.models import ConfirmationMode
from wyzer.tools.base import ToolContext, ToolExecutionError
from wyzer.tools.files import (
    CopyPathArguments,
    CopyPathTool,
    CreateDirectoryArguments,
    CreateDirectoryTool,
    DeletePathTool,
    ListDirectoryArguments,
    ListDirectoryTool,
    MovePathArguments,
    MovePathTool,
    RenamePathArguments,
    RenamePathTool,
)


def context() -> ToolContext:
    return ToolContext(action_id=uuid4(), step_id=uuid4())


def catalog(tmp_path: Path) -> FileCatalog:
    return FileCatalog(tmp_path / "index.sqlite3")


def test_list_directory_and_create_directory(tmp_path: Path) -> None:
    files = catalog(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / ".hidden.txt").write_text("hidden", encoding="utf-8")

    created = CreateDirectoryTool(files).execute(
        CreateDirectoryArguments(path=root / "Project"), context()
    )
    listing = ListDirectoryTool(files).execute(ListDirectoryArguments(path=root), context())

    assert created.created is True
    assert created.kind == "folder"
    assert [entry.name for entry in listing.entries] == ["Project", "notes.txt"]


def test_copy_move_and_rename_do_not_overwrite(tmp_path: Path) -> None:
    files = catalog(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    original = root / "notes.txt"
    original.write_text("hello", encoding="utf-8")

    copied = CopyPathTool(files).execute(
        CopyPathArguments(source=original, destination=root / "copy.txt"), context()
    )
    moved = MovePathTool(files).execute(
        MovePathArguments(source=Path(copied.destination or ""), destination=root / "archive"),
        context(),
    )
    renamed = RenamePathTool(files).execute(
        RenamePathArguments(path=Path(moved.destination or ""), new_name="final.txt"),
        context(),
    )

    final = Path(renamed.destination or "")
    assert original.exists()
    assert final.read_text(encoding="utf-8") == "hello"
    assert final.name == "final.txt"

    existing = root / "occupied.txt"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(ToolExecutionError) as error:
        CopyPathTool(files).execute(
            CopyPathArguments(source=original, destination=existing), context()
        )
    assert error.value.code == "DESTINATION_EXISTS"
    assert existing.read_text(encoding="utf-8") == "keep"


def test_rename_requires_only_a_name(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError):
        RenamePathArguments(path=source, new_name="other/final.txt")


def test_delete_path_is_high_risk_and_always_confirmed(tmp_path: Path) -> None:
    tool = DeletePathTool(catalog(tmp_path))

    assert tool.confirmation == ConfirmationMode.ALWAYS
    assert tool.read_only is False
