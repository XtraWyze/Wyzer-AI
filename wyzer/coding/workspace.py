"""Workspace containment and bounded coding operations."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil


class WorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CodingWorkspace:
    """Resolve all supplied paths against one immutable directory boundary."""

    def __init__(
        self,
        root: str | Path,
        *,
        command_timeout_seconds: float = 60,
        maximum_output_characters: int = 12_000,
    ) -> None:
        requested = Path(root).expanduser()
        if not requested.is_dir():
            raise WorkspaceError("INVALID_WORKSPACE", f"Workspace is not a directory: {requested}")
        self.root = requested.resolve(strict=True)
        self.command_timeout_seconds = command_timeout_seconds
        self.maximum_output_characters = maximum_output_characters
        self.inspected_files: set[Path] = set()
        self.changed_files: set[str] = set()
        self.commands_run: list[dict[str, Any]] = []
        self._process: subprocess.Popen[bytes] | None = None

    def resolve(self, supplied: str | Path = ".", *, must_exist: bool = False) -> Path:
        raw = Path(supplied)
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve(strict=must_exist)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise WorkspaceError(
                "PATH_OUTSIDE_WORKSPACE",
                f"Path must remain inside the assigned workspace: {supplied}",
            ) from error
        return resolved

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        return path.relative_to(root).as_posix()

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(128_000):
                digest.update(chunk)
        return digest.hexdigest()

    def list_directory(self, path: str = ".", maximum_entries: int = 200) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if not target.is_dir():
            raise WorkspaceError("NOT_A_DIRECTORY", f"Not a directory: {path}")
        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())):
            if len(entries) >= maximum_entries:
                break
            safe = self.resolve(item, must_exist=True)
            entries.append(
                {
                    "path": self._relative(safe, self.root),
                    "kind": "directory" if safe.is_dir() else "file",
                    **({"size": safe.stat().st_size} if safe.is_file() else {}),
                }
            )
        return {
            "path": self._relative(target, self.root) or ".",
            "entries": entries,
            "truncated": len(entries) >= maximum_entries,
        }

    def read_file(self, path: str, start_line: int = 1, maximum_lines: int = 400) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if not target.is_file():
            raise WorkspaceError("NOT_A_FILE", f"Not a file: {path}")
        maximum_read_bytes = 2_000_000
        with target.open("rb") as source:
            raw = source.read(maximum_read_bytes + 1)
        file_truncated = len(raw) > maximum_read_bytes
        raw = raw[:maximum_read_bytes]
        if b"\x00" in raw[:8_192]:
            raise WorkspaceError("BINARY_FILE", f"Refusing to read binary file: {path}")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        selected = lines[start_line - 1 : start_line - 1 + maximum_lines]
        content = "".join(selected)
        content_truncated = len(content) > 20_000
        content = content[:20_000]
        self.inspected_files.add(target)
        return {
            "path": self._relative(target, self.root),
            "content": content,
            "start_line": start_line,
            "end_line": start_line + max(0, len(selected) - 1),
            "total_lines_in_bounded_read": len(lines),
            "truncated": (
                file_truncated
                or content_truncated
                or start_line - 1 + len(selected) < len(lines)
            ),
            "sha256": self._file_sha256(target),
        }

    def search(
        self,
        query: str,
        path: str = ".",
        glob: str | None = None,
        maximum_results: int = 100,
    ) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        executable = shutil.which("rg")
        if executable:
            args = [executable, "-n", "--no-heading", "--color", "never", "-F"]
            if glob:
                args.extend(["--glob", glob])
            args.extend(["--", query, str(target)])
            with tempfile.TemporaryFile() as output:
                try:
                    subprocess.run(
                        args,
                        cwd=self.root,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        timeout=min(self.command_timeout_seconds, 30),
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    raise WorkspaceError("SEARCH_TIMEOUT", "Repository search timed out.") from error
                output.seek(0)
                raw_output = output.read(maximum_results * 2_001 + 1)
            output_truncated = len(raw_output) > maximum_results * 2_001
            all_lines = raw_output.decode("utf-8", errors="replace").splitlines()
            lines = all_lines[:maximum_results]
            normalized = [self._normalize_search_line(line) for line in lines]
            return {
                "query": query,
                "results": normalized,
                "count": len(normalized),
                "truncated": output_truncated or len(all_lines) > maximum_results,
            }
        return self._python_search(query, target, glob, maximum_results)

    def _normalize_search_line(self, line: str) -> str:
        root = str(self.root)
        return line.replace(root + os.sep, "").replace(root + "/", "")[:2_000]

    def _python_search(
        self, query: str, target: Path, glob: str | None, maximum_results: int
    ) -> dict[str, Any]:
        results: list[str] = []
        candidates = [target] if target.is_file() else target.rglob("*")
        for candidate in candidates:
            if len(results) >= maximum_results:
                break
            if not candidate.is_file() or (glob and not fnmatch.fnmatch(candidate.name, glob)):
                continue
            safe = self.resolve(candidate, must_exist=True)
            try:
                with safe.open("rb") as source:
                    raw = source.read(2_000_001)
                if b"\x00" in raw[:8_192]:
                    continue
                for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if query in line:
                        results.append(f"{self._relative(safe, self.root)}:{number}:{line[:1_000]}")
                        if len(results) >= maximum_results:
                            break
            except OSError:
                continue
        return {
            "query": query,
            "results": results,
            "count": len(results),
            "truncated": len(results) >= maximum_results,
        }

    def write_file(self, path: str, content: str, expected_sha256: str | None = None) -> dict[str, Any]:
        target = self.resolve(path)
        if target.exists() and target not in self.inspected_files:
            raise WorkspaceError("FILE_NOT_INSPECTED", "Read an existing file before replacing it.")
        if target.exists() and expected_sha256 is not None:
            actual = self._sha256(target.read_bytes())
            if actual != expected_sha256:
                raise WorkspaceError("FILE_CHANGED", "The file changed since it was inspected.")
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        self._atomic_write(target, encoded)
        relative = self._relative(target, self.root)
        self.changed_files.add(relative)
        self.inspected_files.add(target)
        return {"path": relative, "changed": True, "sha256": self._sha256(encoded)}

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_occurrences: int = 1,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if target not in self.inspected_files:
            raise WorkspaceError("FILE_NOT_INSPECTED", "Read the file before editing it.")
        raw = target.read_bytes()
        actual_sha = self._sha256(raw)
        if expected_sha256 is not None and actual_sha != expected_sha256:
            raise WorkspaceError("FILE_CHANGED", "The file changed since it was inspected.")
        text = raw.decode("utf-8")
        occurrences = text.count(old_text)
        if occurrences != expected_occurrences:
            raise WorkspaceError(
                "OCCURRENCE_MISMATCH",
                f"Expected {expected_occurrences} exact matches but found {occurrences}.",
            )
        updated = text.replace(old_text, new_text)
        changed = updated != text
        encoded = updated.encode("utf-8")
        if changed:
            self._atomic_write(target, encoded)
            self.changed_files.add(self._relative(target, self.root))
        return {
            "path": self._relative(target, self.root),
            "changed": changed,
            "occurrences_changed": occurrences,
            "sha256": self._sha256(encoded),
        }

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()

    async def run_command(
        self,
        argv: list[str],
        cwd: str = ".",
        timeout_seconds: float | None = None,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        if not argv or not argv[0].strip():
            raise WorkspaceError("INVALID_COMMAND", "Command argv cannot be empty.")
        if Path(argv[0]).name != argv[0]:
            raise WorkspaceError("INVALID_COMMAND", "Use an executable name, not an executable path.")
        working = self.resolve(cwd, must_exist=True)
        if not working.is_dir():
            raise WorkspaceError("INVALID_CWD", f"Command cwd is not a directory: {cwd}")
        self._validate_argument_paths(argv[1:], working)
        timeout = min(timeout_seconds or self.command_timeout_seconds, self.command_timeout_seconds)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        timed_out = False
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=working,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=flags,
                )
            except OSError as error:
                raise WorkspaceError("COMMAND_START_FAILED", str(error)) from error
            self._process = process
            stdin_task: asyncio.Task[None] | None = None
            try:
                if stdin is not None and process.stdin is not None:
                    stdin_task = asyncio.create_task(
                        asyncio.to_thread(self._write_process_stdin, process, stdin)
                    )
                await asyncio.to_thread(process.wait, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                await self._terminate_process(process)
            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise
            finally:
                self._process = None
                if stdin_task is not None:
                    with suppress(Exception):
                        await stdin_task
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_raw = stdout_file.read(self.maximum_output_characters + 1)
            stderr_raw = stderr_file.read(self.maximum_output_characters + 1)
        stdout_truncated = len(stdout_raw) > self.maximum_output_characters
        stderr_truncated = len(stderr_raw) > self.maximum_output_characters
        stdout = stdout_raw[: self.maximum_output_characters].decode("utf-8", errors="replace")
        stderr = stderr_raw[: self.maximum_output_characters].decode("utf-8", errors="replace")
        result = {
            "argv": argv,
            "cwd": self._relative(working, self.root) or ".",
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": stdout_truncated or stderr_truncated,
            "timed_out": timed_out,
            "stdin_provided": stdin is not None,
        }
        self.commands_run.append(
            {
                "argv": [item[:500] for item in argv[:20]],
                "cwd": result["cwd"],
                "exit_code": process.returncode,
                "timed_out": timed_out,
                "stdin_provided": stdin is not None,
            }
        )
        return result

    def _validate_argument_paths(self, arguments: list[str], working: Path) -> None:
        for argument in arguments:
            value = argument.split("=", 1)[-1] if "=" in argument else argument
            candidate = Path(value)
            if candidate.is_absolute():
                self.resolve(candidate)
            elif ".." in candidate.parts:
                raise WorkspaceError(
                    "PATH_OUTSIDE_WORKSPACE", "Command arguments may not traverse outside workspace."
                )
            elif ("/" in value or "\\" in value or (working / candidate).exists()):
                self.resolve(working / candidate)

    async def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        await asyncio.to_thread(self._terminate_process_tree, process.pid)
        with suppress(OSError):
            process.kill()
        with suppress(Exception):
            await asyncio.to_thread(process.wait, timeout=2)

    @staticmethod
    def _write_process_stdin(process: subprocess.Popen[bytes], content: str) -> None:
        if process.stdin is None:
            return
        with suppress(OSError):
            process.stdin.write(content.encode("utf-8"))
            process.stdin.close()

    @staticmethod
    def _terminate_process_tree(process_id: int) -> None:
        with suppress(psutil.Error):
            parent = psutil.Process(process_id)
            children = parent.children(recursive=True)
            for child in reversed(children):
                with suppress(psutil.Error):
                    child.kill()
            with suppress(psutil.Error):
                parent.kill()
            psutil.wait_procs([*children, parent], timeout=2)

    def cancel_command(self) -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return False
        self._terminate_process_tree(process.pid)
        return True

    async def git_status(self) -> dict[str, Any]:
        return await self.run_command(["git", "status", "--short", "--branch"])

    async def git_diff(self, staged: bool = False, path: str | None = None) -> dict[str, Any]:
        argv = ["git", "diff"]
        if staged:
            argv.append("--cached")
        if path:
            safe = self.resolve(path)
            argv.extend(["--", self._relative(safe, self.root)])
        return await self.run_command(argv)
