"""Model-callable local file discovery tools."""

from __future__ import annotations

import codecs
import hashlib
import os
import shutil
import tempfile
import time
from contextlib import suppress
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

_EXACT_PATH_DESCRIPTION = (
    "Exact absolute local path. For a common user folder, use its location from "
    "CONTEXT_JSON user_folders; do not use a bare relative name such as Desktop."
)


class SearchFilesArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    search_content: bool = True
    limit: int = Field(default=20, ge=1, le=100)


class ReadTextFileArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    maximum_characters: int = Field(default=20_000, ge=100, le=100_000)


class WriteTextFileArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    content: str = Field(max_length=1_000_000)
    overwrite: bool = Field(
        default=False,
        description="Replace an existing text file; requires confirmation.",
    )
    create_parents: bool = False


class EditTextFileArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    old_text: str = Field(min_length=1, max_length=500_000)
    new_text: str = Field(max_length=500_000)
    expected_occurrences: int = Field(default=1, ge=1, le=1_000)
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Optional SHA-256 of the file bytes that must still match.",
    )


class AppendTextFileArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    content: str = Field(max_length=1_000_000)
    create: bool = False
    create_parents: bool = False


class RefreshFileIndexArguments(ToolArguments):
    pass


class DeepScanFileIndexArguments(ToolArguments):
    include_content: bool = Field(
        default=True,
        description="Also index bounded text content, which makes the scan take longer.",
    )


class ListDirectoryArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    include_hidden: bool = False
    limit: int = Field(default=100, ge=1, le=500)


class CreateDirectoryArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    parents: bool = True


class CopyPathArguments(ToolArguments):
    source: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    destination: Path = Field(description=_EXACT_PATH_DESCRIPTION)


class MovePathArguments(ToolArguments):
    source: Path = Field(description=_EXACT_PATH_DESCRIPTION)
    destination: Path = Field(description=_EXACT_PATH_DESCRIPTION)


class RenamePathArguments(ToolArguments):
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)
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
    path: Path = Field(description=_EXACT_PATH_DESCRIPTION)


class OpenIndexedFolderArguments(ToolArguments):
    query: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Literal proper name identifying the user's requested folder or project. Copy it "
            "exactly from the request; exclude possessives, determiners, and the type word."
        ),
    )
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


class TextMutationResult(BaseModel):
    operation: str
    path: str
    kind: str = "file"
    created: bool
    overwritten: bool = False
    occurrences_changed: int | None = None
    characters_written: int
    sha256: str
    summary: str


class FileIndexResult(BaseModel):
    scan_type: str
    files: int
    content_files: int
    skipped: int
    errors: int
    content_read: bool
    complete: bool


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


def _resolve_text_mutation_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        if expanded.is_symlink():
            _reject_symlink(expanded)
        resolved = expanded.resolve(strict=False)
    except ToolExecutionError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ToolExecutionError(
            "INVALID_PATH",
            "The requested file path is invalid.",
            details={"path": str(path)},
        ) from error
    if not resolved.name:
        raise ToolExecutionError(
            "INVALID_PATH",
            "The requested path must identify a file, not a drive or directory root.",
            details={"path": str(resolved)},
        )
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        for part in resolved.parts[1:]:
            if (
                any(character in part for character in '<>:"|?*')
                or part.rstrip(" .") != part
                or part.split(".", 1)[0].upper() in reserved
            ):
                raise ToolExecutionError(
                    "INVALID_PATH",
                    "The requested file path contains a name Windows does not allow.",
                    details={"path": str(resolved)},
                )
    _assert_mutable(resolved)
    if FileCatalog._sensitive(resolved):
        raise ToolExecutionError(
            "SENSITIVE_PATH",
            "Wyzer will not write credential, key, or other sensitive files.",
            details={"path": str(resolved)},
        )
    return resolved


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


_MAX_TEXT_MUTATION_BYTES = 10_000_000
_TEXT_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _read_existing_text(path: Path) -> tuple[bytes, str, str, bytes]:
    if not path.exists():
        raise ToolExecutionError(
            "PATH_NOT_FOUND",
            f"The file does not exist: {path}",
            details={"path": str(path)},
        )
    if not path.is_file():
        raise ToolExecutionError(
            "NOT_A_FILE",
            "The requested path is not a file.",
            details={"path": str(path)},
        )
    _reject_symlink(path)
    try:
        if path.stat().st_size > _MAX_TEXT_MUTATION_BYTES:
            raise ToolExecutionError(
                "FILE_TOO_LARGE",
                "The file is too large for a bounded text mutation.",
                details={"path": str(path), "maximum_bytes": _MAX_TEXT_MUTATION_BYTES},
            )
        raw = path.read_bytes()
    except ToolExecutionError:
        raise
    except (PermissionError, OSError) as error:
        raise ToolExecutionError(
            "FILE_READ_DENIED", str(error), details={"path": str(path)}
        ) from error

    encoding = "utf-8"
    bom = b""
    payload = raw
    for candidate_bom, candidate_encoding in _TEXT_BOMS:
        if raw.startswith(candidate_bom):
            bom = candidate_bom
            encoding = candidate_encoding
            payload = raw[len(candidate_bom) :]
            break
    try:
        text = payload.decode(encoding)
    except UnicodeDecodeError as error:
        raise ToolExecutionError(
            "NOT_TEXT_FILE",
            "The file is not valid supported text and was not modified.",
            details={"path": str(path)},
        ) from error
    controls = sum(
        ord(character) < 32 and character not in "\t\n\r"
        for character in text
    )
    if "\x00" in text or (text and controls / len(text) > 0.01):
        raise ToolExecutionError(
            "NOT_TEXT_FILE",
            "The file appears to be binary and was not modified.",
            details={"path": str(path)},
        )
    return raw, text, encoding, bom


def _encode_text(text: str, encoding: str, bom: bytes) -> bytes:
    try:
        return bom + text.encode(encoding)
    except UnicodeEncodeError as error:
        raise ToolExecutionError(
            "TEXT_ENCODING_FAILED",
            f"The updated text cannot be represented with the file's {encoding} encoding.",
        ) from error


def _ensure_parent(path: Path, create_parents: bool) -> None:
    parent = path.parent
    if parent.is_dir():
        return
    if parent.exists():
        raise ToolExecutionError(
            "PARENT_NOT_DIRECTORY",
            "The parent path is not a directory.",
            details={"path": str(parent)},
        )
    if not create_parents:
        raise ToolExecutionError(
            "PARENT_NOT_FOUND",
            "The parent directory does not exist; set create_parents to create it explicitly.",
            details={"path": str(parent)},
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as error:
        raise ToolExecutionError(
            "DIRECTORY_CREATE_FAILED", str(error), details={"path": str(parent)}
        ) from error


def _write_new_file(path: Path, content: bytes) -> None:
    created = False
    try:
        with path.open("xb") as output:
            created = True
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as error:
        raise ToolExecutionError(
            "FILE_EXISTS",
            "The file already exists. Set overwrite only when replacement is intended.",
            details={"path": str(path)},
        ) from error
    except (PermissionError, OSError) as error:
        if created:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        raise ToolExecutionError(
            "FILE_WRITE_FAILED", str(error), details={"path": str(path)}
        ) from error


def _atomic_replace_if_unchanged(path: Path, original: bytes, replacement: bytes) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        shutil.copymode(path, temporary)
        try:
            current = path.read_bytes()
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise ToolExecutionError(
                "FILE_CHANGED",
                "The file changed or became unavailable before the update; nothing was replaced.",
                retryable=True,
                details={"path": str(path)},
            ) from error
        if current != original:
            raise ToolExecutionError(
                "FILE_CHANGED",
                "The file changed before the update; nothing was replaced.",
                retryable=True,
                details={"path": str(path)},
            )
        os.replace(temporary, path)
        temporary = None
    except ToolExecutionError:
        raise
    except (PermissionError, OSError) as error:
        raise ToolExecutionError(
            "FILE_WRITE_FAILED", str(error), details={"path": str(path)}
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


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
        "Find and open an indexed folder/project. Copy the requested name exactly into query; "
        "optionally place its window on a specified monitor."
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
        target = Path(
            dominant_location([match.path for match in matches], arguments.query)
        )
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


class WriteTextFileTool(FileToolBase, Tool[WriteTextFileArguments, TextMutationResult]):
    name = "write_text_file"
    description = "Create a UTF-8 text file, or explicitly replace an existing text file."
    arguments_type = WriteTextFileArguments
    result_type = TextMutationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.CONDITIONAL

    def execute(
        self, arguments: WriteTextFileArguments, context: ToolContext
    ) -> TextMutationResult:
        del context
        path = _resolve_text_mutation_path(arguments.path)
        _ensure_parent(path, arguments.create_parents)
        existed = path.exists()
        if existed and not arguments.overwrite:
            raise ToolExecutionError(
                "FILE_EXISTS",
                "The file already exists. Set overwrite only when replacement is intended.",
                details={"path": str(path)},
            )
        if existed:
            original, _, encoding, bom = _read_existing_text(path)
            replacement = _encode_text(arguments.content, encoding, bom)
            _atomic_replace_if_unchanged(path, original, replacement)
        else:
            replacement = _encode_text(arguments.content, "utf-8", b"")
            _write_new_file(path, replacement)
        digest = hashlib.sha256(replacement).hexdigest()
        return TextMutationResult(
            operation="write_text_file",
            path=str(path),
            created=not existed,
            overwritten=existed,
            characters_written=len(arguments.content),
            sha256=digest,
            summary=("Created text file." if not existed else "Replaced existing text file."),
        )


class EditTextFileTool(FileToolBase, Tool[EditTextFileArguments, TextMutationResult]):
    name = "edit_text_file"
    description = "Replace an exact text occurrence in an existing file; fail on count mismatch."
    arguments_type = EditTextFileArguments
    result_type = TextMutationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: EditTextFileArguments, context: ToolContext
    ) -> TextMutationResult:
        del context
        path = _resolve_text_mutation_path(arguments.path)
        original, text, encoding, bom = _read_existing_text(path)
        original_digest = hashlib.sha256(original).hexdigest()
        if (
            arguments.expected_sha256 is not None
            and arguments.expected_sha256.casefold() != original_digest
        ):
            raise ToolExecutionError(
                "FILE_CHANGED",
                "The file no longer matches the expected SHA-256; nothing was modified.",
                retryable=True,
                details={"path": str(path), "actual_sha256": original_digest},
            )
        occurrences = text.count(arguments.old_text)
        if occurrences == 0:
            raise ToolExecutionError(
                "TEXT_NOT_FOUND",
                "The exact old_text was not found; nothing was modified.",
                retryable=True,
                details={"path": str(path), "expected_occurrences": arguments.expected_occurrences},
            )
        if occurrences != arguments.expected_occurrences:
            raise ToolExecutionError(
                "OCCURRENCE_COUNT_MISMATCH",
                "The exact old_text occurrence count did not match; nothing was modified.",
                retryable=True,
                details={
                    "path": str(path),
                    "expected_occurrences": arguments.expected_occurrences,
                    "actual_occurrences": occurrences,
                },
            )
        updated = text.replace(arguments.old_text, arguments.new_text)
        replacement = _encode_text(updated, encoding, bom)
        _atomic_replace_if_unchanged(path, original, replacement)
        return TextMutationResult(
            operation="edit_text_file",
            path=str(path),
            created=False,
            occurrences_changed=occurrences,
            characters_written=len(updated),
            sha256=hashlib.sha256(replacement).hexdigest(),
            summary=f"Replaced {occurrences} exact occurrence(s).",
        )


class AppendTextFileTool(FileToolBase, Tool[AppendTextFileArguments, TextMutationResult]):
    name = "append_text_file"
    description = "Append text without changing existing content; create only when explicitly set."
    arguments_type = AppendTextFileArguments
    result_type = TextMutationResult
    risk_level = RiskLevel.MEDIUM
    read_only = False

    def execute(
        self, arguments: AppendTextFileArguments, context: ToolContext
    ) -> TextMutationResult:
        del context
        path = _resolve_text_mutation_path(arguments.path)
        existed = path.exists()
        if not existed and not arguments.create:
            raise ToolExecutionError(
                "PATH_NOT_FOUND",
                "The file does not exist. Set create only when creating it is intended.",
                details={"path": str(path)},
            )
        _ensure_parent(path, arguments.create_parents)
        if existed:
            original, _, encoding, _ = _read_existing_text(path)
            appended = _encode_text(arguments.content, encoding, b"")
            replacement = original + appended
            _atomic_replace_if_unchanged(path, original, replacement)
        else:
            replacement = _encode_text(arguments.content, "utf-8", b"")
            _write_new_file(path, replacement)
        return TextMutationResult(
            operation="append_text_file",
            path=str(path),
            created=not existed,
            characters_written=len(arguments.content),
            sha256=hashlib.sha256(replacement).hexdigest(),
            summary=("Created text file with appended text." if not existed else "Appended text."),
        )


class RefreshFileIndexTool(FileToolBase, Tool[RefreshFileIndexArguments, FileIndexResult]):
    name = "refresh_file_index"
    description = (
        "Run a quick, bounded metadata-only refresh of common user folders. Use this for a "
        "fast index update; use deep_scan_file_index only for a full rebuild."
    )
    arguments_type = RefreshFileIndexArguments
    result_type = FileIndexResult
    risk_level = RiskLevel.LOW
    read_only = False
    default_timeout_seconds = 30

    def execute(
        self, arguments: RefreshFileIndexArguments, context: ToolContext
    ) -> FileIndexResult:
        del arguments, context
        stats = self.catalog.quick_refresh()
        return FileIndexResult(
            scan_type="quick",
            files=stats.files,
            content_files=stats.content_files,
            skipped=stats.skipped,
            errors=stats.errors,
            content_read=False,
            complete=stats.complete,
        )


class DeepScanFileIndexTool(
    FileToolBase, Tool[DeepScanFileIndexArguments, FileIndexResult]
):
    name = "deep_scan_file_index"
    description = (
        "Rebuild the file index across all local drives. This can take several minutes and "
        "must only run after the user confirms the dedicated deep scan."
    )
    arguments_type = DeepScanFileIndexArguments
    result_type = FileIndexResult
    risk_level = RiskLevel.MEDIUM
    read_only = False
    confirmation = ConfirmationMode.ALWAYS
    default_timeout_seconds = 3600

    def execute(
        self, arguments: DeepScanFileIndexArguments, context: ToolContext
    ) -> FileIndexResult:
        del context
        stats = self.catalog.refresh(include_content=arguments.include_content)
        return FileIndexResult(
            scan_type="deep",
            files=stats.files,
            content_files=stats.content_files,
            skipped=stats.skipped,
            errors=stats.errors,
            content_read=arguments.include_content,
            complete=stats.complete,
        )


class FileToolPack:
    """Built-in local file discovery capability pack."""

    name = "files"
    description = (
        "open named local folders/projects; search/read files; write/edit/append text; refresh the "
        "file index; create/copy/move/rename/delete paths."
    )
    activation_name = "file"

    def __init__(self, catalog: FileCatalog, backend: WindowsSystemBackend) -> None:
        self.catalog = catalog
        self.backend = backend

    def create_tools(self) -> tuple[Tool[Any, Any], ...]:
        return (
            SearchFilesTool(self.catalog),
            ListDirectoryTool(self.catalog),
            ReadTextFileTool(self.catalog),
            WriteTextFileTool(self.catalog),
            EditTextFileTool(self.catalog),
            AppendTextFileTool(self.catalog),
            CreateDirectoryTool(self.catalog),
            CopyPathTool(self.catalog),
            MovePathTool(self.catalog),
            RenamePathTool(self.catalog),
            DeletePathTool(self.catalog),
            RefreshFileIndexTool(self.catalog),
            DeepScanFileIndexTool(self.catalog),
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
