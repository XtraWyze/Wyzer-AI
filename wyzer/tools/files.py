"""Model-callable local file discovery tools."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, model_validator
from send2trash import send2trash

from wyzer.desktop.system import WindowsSystemBackend
from wyzer.files import FileCatalog
from wyzer.files.paths import dominant_location
from wyzer.models import (
    ConfirmationMode,
    MonitorDestination,
    RiskLevel,
    ToolArguments,
    WindowInfo,
)
from wyzer.tools.base import Tool, ToolContext, ToolExecutionError


class SearchFilesArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    search_content: bool = True
    limit: int = Field(default=20, ge=1, le=100)


class ReadTextFileArguments(ToolArguments):
    path: Path
    maximum_characters: int = Field(default=20_000, ge=100, le=100_000)


class RefreshFileIndexArguments(ToolArguments):
    include_content: bool = True


class ListDirectoryArguments(ToolArguments):
    path: Path
    include_hidden: bool = False
    limit: int = Field(default=100, ge=1, le=500)


class CreateDirectoryArguments(ToolArguments):
    path: Path
    parents: bool = True


class CopyPathArguments(ToolArguments):
    source: Path
    destination: Path


class MovePathArguments(ToolArguments):
    source: Path
    destination: Path


class RenamePathArguments(ToolArguments):
    path: Path
    new_name: str = Field(
        min_length=1,
        max_length=255,
        description="New file or folder name only, not a full path.",
    )

    @model_validator(mode="after")
    def validate_name(self) -> RenamePathArguments:
        if self.new_name in {".", ".."} or Path(self.new_name).name != self.new_name:
            raise ValueError("new_name must be a single file or folder name")
        if any(character in self.new_name for character in '<>:"/\\|?*'):
            raise ValueError("new_name contains characters Windows does not allow")
        return self


class DeletePathArguments(ToolArguments):
    path: Path


class OpenIndexedFolderArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    destination: MonitorDestination | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_monitor(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "destination" not in normalized and "monitor" in normalized:
            monitor = normalized.pop("monitor")
            if isinstance(monitor, str):
                compact = " ".join(monitor.strip().casefold().split())
                relation = {
                    "other": "other",
                    "the other": "other",
                    "primary": "primary",
                    "left": "left",
                    "right": "right",
                    "above": "above",
                    "below": "below",
                    "nearest": "nearest",
                    "previous": "previous",
                    "back": "previous",
                }.get(compact)
                if relation is not None:
                    normalized["destination"] = {"relation": relation}
                else:
                    number_text = compact.removeprefix("monitor ").removeprefix("display ")
                    normalized["destination"] = (
                        {"number": int(number_text)}
                        if number_text.isdigit()
                        else {"device_name": monitor.strip()}
                    )
        return normalized


class FileSearchResult(BaseModel):
    query: str
    preferred_location: str | None = None
    matches: list[dict[str, object]]
    count: int


class TextFileResult(BaseModel):
    path: str
    content: str
    truncated: bool


class FileIndexResult(BaseModel):
    files: int
    content_files: int
    skipped: int
    errors: int
    content_read: bool


class DirectoryEntry(BaseModel):
    name: str
    path: str
    kind: str
    size: int | None = None
    modified_ns: int | None = None


class DirectoryListingResult(BaseModel):
    path: str
    entries: list[DirectoryEntry]
    count: int
    truncated: bool


class PathOperationResult(BaseModel):
    operation: str
    source: str | None = None
    destination: str | None = None
    path: str | None = None
    kind: str
    created: bool | None = None
    recycled: bool = False


class IndexedFolderOpenResult(BaseModel):
    query: str
    target: str
    target_kind: str = "folder"
    matches: list[dict[str, object]]
    window: WindowInfo | None = None
    command_sent: bool = True
    verified: bool
    evidence: dict[str, object]
    warnings: list[str]


class FileToolBase:
    def __init__(self, catalog: FileCatalog) -> None:
        self.catalog = catalog


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _kind(path: Path) -> str:
    if path.is_dir():
        return "folder"
    if path.is_file():
        return "file"
    return "path"


def _protected_mutation_roots() -> tuple[Path, ...]:
    candidates = [
        os.environ.get("SYSTEMROOT"),
        os.environ.get("WINDIR"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMDATA"),
    ]
    roots: list[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        resolved = Path(candidate).expanduser().resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _is_same_or_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_mutable(path: Path) -> None:
    if path == Path(path.anchor):
        raise ToolExecutionError(
            "PROTECTED_PATH",
            "Wyzer will not modify the root of a drive.",
            details={"path": str(path)},
        )
    for root in _protected_mutation_roots():
        if _is_same_or_child(path, root):
            raise ToolExecutionError(
                "PROTECTED_PATH",
                "Wyzer will not modify protected Windows or program directories.",
                details={"path": str(path), "protected_root": str(root)},
            )


def _require_existing(path: Path) -> None:
    if not path.exists():
        raise ToolExecutionError(
            "PATH_NOT_FOUND",
            f"The path does not exist: {path}",
            details={"path": str(path)},
        )


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ToolExecutionError(
            "SYMLINK_NOT_SUPPORTED",
            "File management does not modify symbolic links.",
            details={"path": str(path)},
        )


def _resolve_destination(source: Path, requested: Path) -> Path:
    destination = _resolve_path(requested)
    if destination.exists() and destination.is_dir():
        destination = destination / source.name
    if destination.exists():
        raise ToolExecutionError(
            "DESTINATION_EXISTS",
            "The destination already exists. Wyzer will not overwrite files or folders implicitly.",
            details={"destination": str(destination)},
        )
    _assert_mutable(destination)
    return destination


class SearchFilesTool(FileToolBase, Tool[SearchFilesArguments, FileSearchResult]):
    name = "search_files"
    description = "Search indexed local files by name, path, or safe text content."
    arguments_type = SearchFilesArguments
    result_type = FileSearchResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: SearchFilesArguments, context: ToolContext) -> FileSearchResult:
        del context
        matches = self.catalog.search(
            arguments.query, content=arguments.search_content, limit=arguments.limit
        )
        values = [
            {
                "path": match.path,
                "name": match.name,
                "extension": match.extension,
                "size": match.size,
                "modified_ns": match.modified_ns,
                "content_indexed": match.content_match,
            }
            for match in matches
        ]
        paths = [str(value["path"]) for value in values]
        location = dominant_location(paths, arguments.query) if paths else None
        return FileSearchResult(
            query=arguments.query,
            preferred_location=location,
            matches=values,
            count=len(values),
        )


class ListDirectoryTool(FileToolBase, Tool[ListDirectoryArguments, DirectoryListingResult]):
    name = "list_directory"
    description = (
        "List files and folders in an exact local directory."
    )
    arguments_type = ListDirectoryArguments
    result_type = DirectoryListingResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(
        self, arguments: ListDirectoryArguments, context: ToolContext
    ) -> DirectoryListingResult:
        del context
        path = _resolve_path(arguments.path)
        if not path.is_dir():
            raise ToolExecutionError(
                "DIRECTORY_NOT_FOUND",
                f"The directory does not exist: {path}",
                details={"path": str(path)},
            )
        entries: list[DirectoryEntry] = []
        truncated = False
        try:
            children = sorted(
                path.iterdir(),
                key=lambda child: (not child.is_dir(), child.name.casefold()),
            )
            if not arguments.include_hidden:
                children = [child for child in children if not child.name.startswith(".")]
            truncated = len(children) > arguments.limit
            for child in children[: arguments.limit]:
                try:
                    stat = child.stat()
                except OSError:
                    stat = None
                entries.append(
                    DirectoryEntry(
                        name=child.name,
                        path=str(child),
                        kind=_kind(child),
                        size=(stat.st_size if stat is not None and child.is_file() else None),
                        modified_ns=(stat.st_mtime_ns if stat is not None else None),
                    )
                )
        except (PermissionError, OSError) as error:
            raise ToolExecutionError("DIRECTORY_READ_DENIED", str(error)) from error
        return DirectoryListingResult(
            path=str(path), entries=entries, count=len(entries), truncated=truncated
        )


class CreateDirectoryTool(FileToolBase, Tool[CreateDirectoryArguments, PathOperationResult]):
    name = "create_directory"
    description = (
        "Create a local folder at an exact path."
    )
    arguments_type = CreateDirectoryArguments
    result_type = PathOperationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: CreateDirectoryArguments, context: ToolContext
    ) -> PathOperationResult:
        del context
        path = _resolve_path(arguments.path)
        _assert_mutable(path)
        existed = path.exists()
        if existed and not path.is_dir():
            raise ToolExecutionError(
                "PATH_EXISTS",
                "A file already exists at the requested folder path.",
                details={"path": str(path)},
            )
        try:
            path.mkdir(parents=arguments.parents, exist_ok=True)
        except (PermissionError, OSError) as error:
            raise ToolExecutionError("DIRECTORY_CREATE_FAILED", str(error)) from error
        return PathOperationResult(
            operation="create_directory",
            path=str(path),
            kind="folder",
            created=not existed,
        )


class CopyPathTool(FileToolBase, Tool[CopyPathArguments, PathOperationResult]):
    name = "copy_path"
    description = (
        "Copy a local file or folder without overwriting an existing destination."
    )
    arguments_type = CopyPathArguments
    result_type = PathOperationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: CopyPathArguments, context: ToolContext) -> PathOperationResult:
        del context
        source = _resolve_path(arguments.source)
        _require_existing(source)
        _reject_symlink(source)
        destination = _resolve_destination(source, arguments.destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        except (PermissionError, OSError, shutil.Error) as error:
            raise ToolExecutionError("COPY_FAILED", str(error)) from error
        return PathOperationResult(
            operation="copy",
            source=str(source),
            destination=str(destination),
            kind=_kind(destination),
        )


class MovePathTool(FileToolBase, Tool[MovePathArguments, PathOperationResult]):
    name = "move_path"
    description = (
        "Move a local file or folder without overwriting an existing destination."
    )
    arguments_type = MovePathArguments
    result_type = PathOperationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: MovePathArguments, context: ToolContext) -> PathOperationResult:
        del context
        source = _resolve_path(arguments.source)
        _require_existing(source)
        _reject_symlink(source)
        _assert_mutable(source)
        destination = _resolve_destination(source, arguments.destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        except (PermissionError, OSError, shutil.Error) as error:
            raise ToolExecutionError("MOVE_FAILED", str(error)) from error
        return PathOperationResult(
            operation="move",
            source=str(source),
            destination=str(destination),
            kind=_kind(destination),
        )


class RenamePathTool(FileToolBase, Tool[RenamePathArguments, PathOperationResult]):
    name = "rename_path"
    description = (
        "Rename a local file or folder within its parent; never overwrite."
    )
    arguments_type = RenamePathArguments
    result_type = PathOperationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(self, arguments: RenamePathArguments, context: ToolContext) -> PathOperationResult:
        del context
        source = _resolve_path(arguments.path)
        _require_existing(source)
        _reject_symlink(source)
        _assert_mutable(source)
        destination = source.with_name(arguments.new_name)
        if destination.exists():
            raise ToolExecutionError(
                "DESTINATION_EXISTS",
                "A file or folder with that name already exists.",
                details={"destination": str(destination)},
            )
        try:
            source.rename(destination)
        except (PermissionError, OSError) as error:
            raise ToolExecutionError("RENAME_FAILED", str(error)) from error
        return PathOperationResult(
            operation="rename",
            source=str(source),
            destination=str(destination),
            kind=_kind(destination),
        )


class DeletePathTool(FileToolBase, Tool[DeletePathArguments, PathOperationResult]):
    name = "delete_path"
    description = (
        "Send a local file or folder to the Recycle Bin; requires confirmation."
    )
    arguments_type = DeletePathArguments
    result_type = PathOperationResult
    risk_level = RiskLevel.HIGH
    read_only = False
    confirmation = ConfirmationMode.ALWAYS

    def execute(self, arguments: DeletePathArguments, context: ToolContext) -> PathOperationResult:
        del context
        path = _resolve_path(arguments.path)
        _require_existing(path)
        _reject_symlink(path)
        _assert_mutable(path)
        kind = _kind(path)
        try:
            send2trash(str(path))
        except (OSError, PermissionError) as error:
            raise ToolExecutionError("RECYCLE_FAILED", str(error)) from error
        return PathOperationResult(
            operation="delete",
            path=str(path),
            kind=kind,
            recycled=True,
        )


class OpenIndexedFolderTool(
    FileToolBase, Tool[OpenIndexedFolderArguments, IndexedFolderOpenResult]
):
    name = "open_indexed_folder"
    description = (
        "Find and open an indexed folder, optionally on a specified monitor."
    )
    arguments_type = OpenIndexedFolderArguments
    result_type = IndexedFolderOpenResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def __init__(self, catalog: FileCatalog, backend: WindowsSystemBackend) -> None:
        super().__init__(catalog)
        self.backend = backend

    def execute(
        self, arguments: OpenIndexedFolderArguments, context: ToolContext
    ) -> IndexedFolderOpenResult:
        del context
        matches = self.catalog.search(arguments.query, content=True, limit=50)
        if not matches:
            raise ToolExecutionError(
                "INDEXED_FOLDER_NOT_FOUND",
                f"No indexed folder matched {arguments.query}.",
                details={"query": arguments.query},
            )
        values = [
            {
                "path": match.path,
                "name": match.name,
                "extension": match.extension,
            }
            for match in matches
        ]
        target = Path(dominant_location([match.path for match in matches], arguments.query))
        before_handles = {window.handle for window in self.backend.list_windows()}
        self.backend.open_file(target)
        matched_window: WindowInfo | None = None
        deadline = time.monotonic() + float(
            getattr(self.backend, "verification_timeout_seconds", 2.0)
        )
        while time.monotonic() < deadline:
            candidates = [
                window
                for window in self.backend.list_windows()
                if (window.application or "").casefold().removesuffix(".exe") == "explorer"
                and target.name.casefold() in window.title.casefold()
            ]
            new_candidates = [
                window for window in candidates if window.handle not in before_handles
            ]
            if len(new_candidates) == 1:
                matched_window = new_candidates[0]
                break
            if len(candidates) == 1:
                matched_window = candidates[0]
                break
            time.sleep(0.05)
        verified = matched_window is not None
        moved = False
        move_outcome = None
        if matched_window is not None and arguments.destination is not None:
            move_outcome = self.backend.move_window_to_monitor(
                matched_window.handle, arguments.destination
            )
            moved = move_outcome.verified
            matched_window = next(
                (
                    window
                    for window in self.backend.list_windows()
                    if window.handle == matched_window.handle
                ),
                matched_window,
            )
            verified = verified and moved
        return IndexedFolderOpenResult(
            query=arguments.query,
            target=str(target.resolve()),
            matches=values,
            window=matched_window,
            verified=verified,
            evidence={
                "verification_status": "verified" if verified else "unavailable",
                "predicate": (
                    "folder_window_exists_and_moved"
                    if arguments.destination
                    else "folder_window_exists"
                ),
                "observed": {
                    "window_handle": matched_window.handle if matched_window else None,
                    "match_count": len(values),
                    "requested_destination": (
                        arguments.destination.model_dump(mode="json")
                        if arguments.destination
                        else None
                    ),
                    "target_monitor": (
                        move_outcome.target_monitor.model_dump(mode="json")
                        if move_outcome
                        else None
                    ),
                    "moved": moved if arguments.destination else None,
                    "monitor_id": matched_window.monitor_id if matched_window else None,
                },
            },
            warnings=(
                []
                if verified
                else [
                    "The folder was requested, but its Explorer window or monitor move "
                    "was not verified."
                ]
            ),
        )


class ReadTextFileTool(FileToolBase, Tool[ReadTextFileArguments, TextFileResult]):
    name = "read_text_file"
    description = "Read bounded text from a non-sensitive local file."
    arguments_type = ReadTextFileArguments
    result_type = TextFileResult
    risk_level = RiskLevel.LOW
    read_only = True

    def execute(self, arguments: ReadTextFileArguments, context: ToolContext) -> TextFileResult:
        del context
        try:
            content = self.catalog.read_text(arguments.path, arguments.maximum_characters)
        except (FileNotFoundError, PermissionError, ValueError, OSError) as error:
            raise ToolExecutionError("FILE_READ_DENIED", str(error)) from error
        return TextFileResult(
            path=str(arguments.path.expanduser().resolve()),
            content=content,
            truncated=len(content) >= arguments.maximum_characters,
        )


class RefreshFileIndexTool(FileToolBase, Tool[RefreshFileIndexArguments, FileIndexResult]):
    name = "refresh_file_index"
    description = "Refresh the local index of safe file metadata and text."
    arguments_type = RefreshFileIndexArguments
    result_type = FileIndexResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    default_timeout_seconds = 3600

    def execute(
        self, arguments: RefreshFileIndexArguments, context: ToolContext
    ) -> FileIndexResult:
        del context
        stats = self.catalog.refresh(include_content=arguments.include_content)
        return FileIndexResult(
            files=stats.files,
            content_files=stats.content_files,
            skipped=stats.skipped,
            errors=stats.errors,
            content_read=arguments.include_content,
        )


class FileToolPack:
    """Built-in local file discovery capability pack."""

    name = "files"

    def __init__(self, catalog: FileCatalog, backend: WindowsSystemBackend) -> None:
        self.catalog = catalog
        self.backend = backend

    def create_tools(self) -> tuple[Tool[Any, Any], ...]:
        return (
            SearchFilesTool(self.catalog),
            ListDirectoryTool(self.catalog),
            ReadTextFileTool(self.catalog),
            CreateDirectoryTool(self.catalog),
            CopyPathTool(self.catalog),
            MovePathTool(self.catalog),
            RenamePathTool(self.catalog),
            DeletePathTool(self.catalog),
            RefreshFileIndexTool(self.catalog),
            OpenIndexedFolderTool(self.catalog, self.backend),
        )


def register_file_tools(
    registry: object, catalog: FileCatalog, backend: WindowsSystemBackend
) -> None:
    """Backward-compatible registration helper."""
    pack = FileToolPack(catalog, backend)
    register_pack = getattr(registry, "register_pack", None)
    if callable(register_pack):
        register_pack(pack)
        return
    register = cast(Any, registry).register
    for tool in pack.create_tools():
        register(tool)
