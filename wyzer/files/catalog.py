"""Incremental local file metadata and bounded text-content indexing."""

from __future__ import annotations

import ctypes
import difflib
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

ScanRecord = tuple[str, str, str, int, int, str | None, int]


@dataclass(frozen=True, slots=True)
class FileMatch:
    path: str
    name: str
    extension: str
    size: int
    modified_ns: int
    content_match: bool


@dataclass(frozen=True, slots=True)
class IndexStats:
    files: int
    content_files: int
    skipped: int
    errors: int
    complete: bool = True


@dataclass(frozen=True, slots=True)
class _RootScanResult:
    root: Path
    stats: IndexStats
    observed: set[str]


_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


class FileCatalog:
    TEXT_EXTENSIONS: ClassVar[set[str]] = {
        ".c",
        ".cpp",
        ".css",
        ".csv",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".log",
        ".md",
        ".py",
        ".rst",
        ".toml",
        ".ts",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
    EXCLUDED_DIRECTORIES: ClassVar[set[str]] = {
        "$recycle.bin",
        ".git",
        ".codex",
        ".cache",
        ".tmp",
        ".mypy_cache",
        ".pio",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".wyzer",
        "__pycache__",
        "node_modules",
        "system volume information",
        "windows",
        "windowsapps",
        "winsxs",
        "program files",
        "program files (x86)",
        "programdata",
        "appdata",
        "recovery",
    }
    SENSITIVE_NAMES: ClassVar[set[str]] = {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "known_hosts",
        "login data",
        "cookies",
    }
    SENSITIVE_EXTENSIONS: ClassVar[set[str]] = {".key", ".pem", ".pfx", ".p12", ".kdbx"}
    SEARCH_EXCLUDED_DIRECTORIES: ClassVar[set[str]] = {
        ".cache",
        ".codex",
        ".tmp",
        ".mypy_cache",
        ".pio",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
    }

    def __init__(self, database: Path = Path(".wyzer/file_index.sqlite3")) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        database_key = str(self.database.resolve()).casefold()
        with _REFRESH_LOCKS_GUARD:
            self._refresh_lock = _REFRESH_LOCKS.setdefault(database_key, threading.Lock())
        self._initialize()

    def quick_refresh(
        self,
        roots: list[Path] | None = None,
        *,
        maximum_files: int = 20_000,
        timeout_seconds: float = 5.0,
        maximum_depth: int = 8,
        environ: Mapping[str, str] | None = None,
    ) -> IndexStats:
        """Run a bounded metadata-only scan suitable for application startup.

        An incomplete bounded scan updates everything it observes but deliberately
        avoids pruning old rows. A later complete quick or deep scan can safely
        remove entries that no longer exist.
        """

        selected_roots = roots if roots is not None else self.quick_roots(environ)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        return self.refresh(
            selected_roots,
            include_content=False,
            maximum_files=max(1, maximum_files),
            deadline=deadline,
            maximum_depth=max(0, maximum_depth),
        )

    def refresh(
        self,
        roots: list[Path] | None = None,
        *,
        include_content: bool = True,
        maximum_files: int | None = None,
        deadline: float | None = None,
        maximum_depth: int | None = None,
    ) -> IndexStats:
        roots = self.drive_roots() if roots is None else roots
        if not roots:
            return IndexStats(0, 0, 0, 0)
        resolved_roots = list(dict.fromkeys(root.expanduser().resolve() for root in roots))
        with self._refresh_lock:
            with self._connect() as connection:
                existing = {
                    str(path).casefold(): (int(size), int(modified), bool(has_content))
                    for path, size, modified, has_content in connection.execute(
                        "SELECT path,size,modified_ns,content IS NOT NULL FROM files"
                    )
                }
            batches: queue.Queue[list[ScanRecord] | None] = queue.Queue(maxsize=16)
            totals = IndexStats(0, 0, 0, 0)
            workers = min(len(resolved_roots), max(1, (os.cpu_count() or 2) // 2), 4)
            per_root_limit = (
                max(1, maximum_files // len(resolved_roots))
                if maximum_files is not None
                else None
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        self._scan_root,
                        root,
                        include_content,
                        existing,
                        batches,
                        maximum_files=per_root_limit,
                        deadline=deadline,
                        maximum_depth=maximum_depth,
                    )
                    for root in resolved_roots
                ]
                completed = 0
                with self._connect() as connection:
                    while completed < len(futures):
                        batch = batches.get()
                        if batch is None:
                            completed += 1
                            continue
                        connection.executemany(
                            """
                            INSERT INTO files(path,name,extension,size,modified_ns,content)
                            VALUES(?,?,?,?,?,?)
                            ON CONFLICT(path) DO UPDATE SET
                              name=excluded.name, extension=excluded.extension,
                              size=excluded.size, modified_ns=excluded.modified_ns,
                              content=CASE WHEN ?=0 THEN files.content ELSE excluded.content END
                            """,
                            batch,
                        )
                    results = [future.result() for future in futures]
                    observed = set().union(*(result.observed for result in results))
                    complete_roots = [result.root for result in results if result.stats.complete]
                    for result in results:
                        stats = result.stats
                        totals = IndexStats(
                            totals.files + stats.files,
                            totals.content_files + stats.content_files,
                            totals.skipped + stats.skipped,
                            totals.errors + stats.errors,
                            totals.complete and stats.complete,
                        )
                    stale = [
                        (path,)
                        for path in existing
                        if path not in observed
                        and any(self._path_is_within(path, root) for root in complete_roots)
                    ]
                    connection.executemany("DELETE FROM files WHERE path=?", stale)
            return totals

    def _scan_root(
        self,
        root: Path,
        include_content: bool,
        existing: dict[str, tuple[int, int, bool]],
        output: queue.Queue[list[ScanRecord] | None],
        *,
        maximum_files: int | None,
        deadline: float | None,
        maximum_depth: int | None,
    ) -> _RootScanResult:
        files = content_files = skipped = errors = 0
        complete = True
        seen: set[str] = set()
        batch: list[ScanRecord] = []

        def walk_error(_error: OSError) -> None:
            nonlocal errors, complete
            errors += 1
            complete = False

        try:
            if not root.is_dir():
                return _RootScanResult(root, IndexStats(0, 0, 0, 1, False), seen)
            for directory, names, filenames in os.walk(root, topdown=True, onerror=walk_error):
                if deadline is not None and time.monotonic() >= deadline:
                    complete = False
                    break
                depth = len(Path(directory).relative_to(root).parts)
                if maximum_depth is not None and depth >= maximum_depth:
                    if names:
                        complete = False
                    names.clear()
                names[:] = [name for name in names if not self._excluded_directory(name)]
                for filename in filenames:
                    if maximum_files is not None and files >= maximum_files:
                        complete = False
                        names.clear()
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        complete = False
                        names.clear()
                        break
                    path = Path(directory) / filename
                    if self._sensitive(path):
                        skipped += 1
                        continue
                    try:
                        stat = path.stat()
                        resolved = str(path.resolve())
                        key = resolved.casefold()
                        prior = existing.get(key)
                        unchanged = prior is not None and prior[:2] == (
                            stat.st_size,
                            stat.st_mtime_ns,
                        )
                        refresh_content = int(include_content and not unchanged)
                        content = (
                            self._read_indexable_text(path, stat.st_size)
                            if refresh_content
                            else None
                        )
                        batch.append(
                            (
                                resolved,
                                path.name,
                                path.suffix.casefold(),
                                stat.st_size,
                                stat.st_mtime_ns,
                                content,
                                refresh_content,
                            )
                        )
                        if len(batch) >= 500:
                            output.put(batch)
                            batch = []
                        seen.add(key)
                        files += 1
                        content_files += content is not None or bool(
                            unchanged and prior and prior[2]
                        )
                    except (OSError, UnicodeError):
                        errors += 1
                        complete = False
                if not complete and (
                    (maximum_files is not None and files >= maximum_files)
                    or (deadline is not None and time.monotonic() >= deadline)
                ):
                    break
            if batch:
                output.put(batch)
        finally:
            output.put(None)
        return _RootScanResult(
            root,
            IndexStats(files, content_files, skipped, errors, complete),
            seen,
        )

    def search(self, query: str, *, content: bool = True, limit: int = 20) -> list[FileMatch]:
        terms = [term for term in query.casefold().split() if term]
        if not terms:
            return []
        clauses: list[str] = []
        values: list[object] = []
        for term in terms:
            pattern = f"%{term}%"
            if content:
                clauses.append(
                    "(lower(name) LIKE ? OR lower(path) LIKE ? OR lower(content) LIKE ?)"
                )
                values.extend((pattern, pattern, pattern))
            else:
                clauses.append("(lower(name) LIKE ? OR lower(path) LIKE ?)")
                values.extend((pattern, pattern))
        values.append(limit * 10)
        sql = (
            "SELECT path,name,extension,size,modified_ns, "
            "CASE WHEN content IS NULL THEN 0 ELSE 1 END FROM files WHERE "
            + " AND ".join(clauses)
            + " ORDER BY modified_ns DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
            if not rows:
                rows = self._fuzzy_metadata_search(connection, terms, limit * 10)
        visible = [row for row in rows if self._search_result_visible(str(row[0]))]
        visible = self._rank_search_rows(visible, terms)
        return [
            FileMatch(row[0], row[1], row[2], row[3], row[4], bool(row[5]))
            for row in visible[:limit]
        ]

    @staticmethod
    def _rank_search_rows(
        rows: list[tuple[str, str, str, int, int, int]], terms: list[str]
    ) -> list[tuple[str, str, str, int, int, int]]:
        generic = {"a", "file", "files", "folder", "my", "project", "repo", "repository", "the"}
        useful = [term for term in terms if term not in generic]

        def score(row: tuple[str, str, str, int, int, int]) -> tuple[int, int, int, int]:
            path = Path(row[0])
            metadata = f"{row[1]} {row[0]}".casefold()
            components = [
                "".join(character for character in part.casefold() if character.isalnum())
                for part in path.parts
            ]
            direct_component_matches = sum(
                any(term in component for component in components) for term in useful
            )
            all_metadata = int(bool(useful) and all(term in metadata for term in useful))
            documents_bonus = int(any(part.casefold() == "documents" for part in path.parts))
            return direct_component_matches, all_metadata, documents_bonus, row[4]

        return sorted(rows, key=score, reverse=True)

    @classmethod
    def _search_result_visible(cls, raw_path: str) -> bool:
        path = Path(raw_path)
        return not cls._sensitive(path) and not any(
            part.casefold() in cls.SEARCH_EXCLUDED_DIRECTORIES for part in path.parts
        )

    @staticmethod
    def _fuzzy_metadata_search(
        connection: sqlite3.Connection, terms: list[str], limit: int
    ) -> list[tuple[str, str, str, int, int, int]]:
        """Recover from natural-language extras and approximate project names.

        The normal query intentionally remains precise.  On a miss, use prefixes to
        obtain a bounded candidate set, then rank path components in Python.  This
        handles queries such as ``priussolarcharger project`` for a directory named
        ``PriusSolarController`` without scanning the entire catalog.
        """
        useful = [term for term in terms if term not in {"file", "files", "folder", "project"}]
        prefixes = [term[:5] for term in useful if len(term) >= 4]
        if not prefixes:
            return []
        clauses = " OR ".join("lower(path) LIKE ?" for _ in prefixes)
        candidates = connection.execute(
            "SELECT path,name,extension,size,modified_ns,0 FROM files WHERE "
            + clauses
            + " ORDER BY modified_ns DESC LIMIT 2000",
            [f"%{prefix}%" for prefix in prefixes],
        ).fetchall()
        needle = "".join(character for character in "".join(useful) if character.isalnum())

        def score(row: tuple[str, str, str, int, int, int]) -> float:
            components = (*Path(row[0]).parts, row[1])
            normalized = [
                "".join(character for character in part.casefold() if character.isalnum())
                for part in components
            ]
            similarities = (
                difflib.SequenceMatcher(None, needle, part).ratio() for part in normalized
            )
            return max(similarities, default=0)

        ranked = sorted(candidates, key=lambda row: (score(row), row[4]), reverse=True)
        return ranked[:limit]

    def read_text(self, path: Path, maximum_characters: int = 20_000) -> str:
        resolved = path.expanduser().resolve()
        if self._sensitive(resolved):
            raise PermissionError("Sensitive credential or key files are excluded from reading.")
        if not resolved.is_file():
            raise FileNotFoundError(str(resolved))
        if resolved.stat().st_size > 2_000_000:
            raise ValueError("File is too large for bounded text reading.")
        return resolved.read_text(encoding="utf-8", errors="replace")[:maximum_characters]

    @staticmethod
    def drive_roots() -> list[Path]:
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
        roots: list[Path] = []
        for bit in range(26):
            if not mask & (1 << bit):
                continue
            root = f"{chr(65 + bit)}:\\"
            if int(ctypes.windll.kernel32.GetDriveTypeW(root)) in {2, 3}:
                roots.append(Path(root))
        return roots

    @staticmethod
    def quick_roots(environ: Mapping[str, str] | None = None) -> list[Path]:
        """Return high-value user folders without traversing entire drives."""

        env = os.environ if environ is None else environ
        homes = [env.get("USERPROFILE"), env.get("OneDrive"), env.get("OneDriveConsumer")]
        candidates: list[Path] = []
        for raw_home in homes:
            if not raw_home:
                continue
            home = Path(raw_home).expanduser()
            candidates.extend(
                home / name
                for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
            )
        return list(dict.fromkeys(path.resolve() for path in candidates if path.is_dir()))

    @staticmethod
    def _path_is_within(raw_path: str, root: Path) -> bool:
        path = raw_path.replace("\\", "/").rstrip("/").casefold()
        parent = str(root).replace("\\", "/").rstrip("/").casefold()
        return path == parent or path.startswith(parent + "/")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS files(
                path TEXT PRIMARY KEY COLLATE NOCASE, name TEXT NOT NULL,
                extension TEXT NOT NULL, size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL, content TEXT)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS files_name ON files(name COLLATE NOCASE)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=30)

    @classmethod
    def _excluded_directory(cls, name: str) -> bool:
        return name.casefold() in cls.EXCLUDED_DIRECTORIES

    @classmethod
    def _sensitive(cls, path: Path) -> bool:
        lowered = {part.casefold() for part in path.parts}
        return (
            path.name.casefold() in cls.SENSITIVE_NAMES
            or path.suffix.casefold() in cls.SENSITIVE_EXTENSIONS
            or ".ssh" in lowered
            or "credential" in lowered
            or "password" in lowered
        )

    @classmethod
    def _read_indexable_text(cls, path: Path, size: int) -> str | None:
        if path.suffix.casefold() not in cls.TEXT_EXTENSIONS or size > 1_000_000:
            return None
        return path.read_text(encoding="utf-8", errors="replace")[:100_000]
