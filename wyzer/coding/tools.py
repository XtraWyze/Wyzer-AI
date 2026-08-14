"""Small native-tool schema and dispatcher for the coding context."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from wyzer.coding.workspace import CodingWorkspace, WorkspaceError
from wyzer.models import NativeFunctionDefinition, NativeToolDefinition, ToolArguments
from wyzer.tools.schema import model_parameters


class ListDirectoryArguments(ToolArguments):
    path: str = "."
    maximum_entries: int = Field(default=200, ge=1, le=500)


class ReadFileArguments(ToolArguments):
    path: str
    start_line: int = Field(default=1, ge=1)
    maximum_lines: int = Field(default=400, ge=1, le=1_000)


class SearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    glob: str | None = None
    maximum_results: int = Field(default=100, ge=1, le=300)


class WriteFileArguments(ToolArguments):
    path: str
    content: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EditFileArguments(ToolArguments):
    path: str
    old_text: str
    new_text: str
    expected_occurrences: int = Field(default=1, ge=1, le=1_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RunCommandArguments(ToolArguments):
    argv: list[Annotated[str, Field(min_length=1, max_length=4_000)]] = Field(
        min_length=1, max_length=40
    )
    cwd: str = "."
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    stdin: str | None = Field(
        default=None,
        max_length=20_000,
        description="Optional scripted input for testing an interactive program.",
    )


class GitStatusArguments(ToolArguments):
    pass


class GitDiffArguments(ToolArguments):
    staged: bool = False
    path: str | None = None


_TOOLS: tuple[tuple[str, str, type[ToolArguments]], ...] = (
    ("code_list_directory", "List bounded entries inside the workspace.", ListDirectoryArguments),
    ("code_read_file", "Read bounded UTF-8 lines from a workspace file.", ReadFileArguments),
    ("code_search", "Search repository text with bounded results.", SearchArguments),
    ("code_write_file", "Atomically create or replace an inspected workspace file.", WriteFileArguments),
    ("code_edit_file", "Apply an exact counted edit to an inspected file.", EditFileArguments),
    ("code_run_command", "Run argv without a shell; optional stdin tests interactive programs.", RunCommandArguments),
    ("code_git_status", "Read concise Git working-tree status.", GitStatusArguments),
    ("code_git_diff", "Read a bounded unstaged or staged Git diff.", GitDiffArguments),
)

ARGUMENT_TYPES = {name: arguments for name, _, arguments in _TOOLS}


def coding_native_tools() -> list[NativeToolDefinition]:
    return [
        NativeToolDefinition(
            function=NativeFunctionDefinition(
                name=name,
                description=description,
                parameters=model_parameters(arguments),
            )
        )
        for name, description, arguments in _TOOLS
    ]


async def execute_coding_tool(
    workspace: CodingWorkspace, name: str, raw_arguments: dict[str, Any]
) -> dict[str, Any]:
    arguments_type = ARGUMENT_TYPES.get(name)
    if arguments_type is None:
        return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": name}}
    try:
        arguments = arguments_type.model_validate(raw_arguments).model_dump()
        if name == "code_list_directory":
            data = await _thread(workspace.list_directory, **arguments)
        elif name == "code_read_file":
            data = await _thread(workspace.read_file, **arguments)
        elif name == "code_search":
            data = await _thread(workspace.search, **arguments)
        elif name == "code_write_file":
            data = await _thread(workspace.write_file, **arguments)
        elif name == "code_edit_file":
            data = await _thread(workspace.edit_file, **arguments)
        elif name == "code_run_command":
            data = await workspace.run_command(**arguments)
        elif name == "code_git_status":
            data = await workspace.git_status()
        else:
            data = await workspace.git_diff(**arguments)
        return {"ok": True, "tool": name, "data": data}
    except WorkspaceError as error:
        return {"ok": False, "tool": name, "error": {"code": error.code, "message": str(error)}}
    except Exception as error:
        return {
            "ok": False,
            "tool": name,
            "error": {"code": "TOOL_FAILED", "message": str(error) or type(error).__name__},
        }


async def _thread(function: Any, **arguments: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(function, **arguments)
