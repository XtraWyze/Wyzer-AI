# Coding agent

Wyzer can delegate software-development work to a retained coding session. Delegation remains an
LLM decision: the `coding_agent` capability is described by `ToolRegistry`, and the main model calls
it through native tool calling. Its four compact coordination proxies stay in the default model
view so small models do not create a redundant outer task plan before delegating. There is no
keyword intent router.

## Same provider, separate conversation

`build_assistant()` constructs the configured `ChatProvider` once and gives that exact object to
both `Orchestrator` and `CodingAgentManager`. The coding subsystem therefore uses the same provider,
endpoint, configured model, and Ollama model weights as Wyzer. It does not load a second model or
have a second `[llm]` section.

Sharing a provider does not mean sharing a conversation. Each `CodingSession` owns a bounded list
of coding messages under a coding-specific system prompt. The main conversation receives only the
proxy call, a compact structured result, and a bounded session summary such as the session ID,
workspace, status, changed files, and last verification. Coding file contents, command output, and
the full coding transcript are not merged into Wyzer's chat history.

## Main-process persistence

The four main-model tools are:

- `coding_agent_start`
- `coding_agent_message`
- `coding_agent_status`
- `coding_agent_cancel`

They are registry-owned proxy definitions. `Orchestrator` validates them through the normal
Pydantic and capability-visibility path, then intercepts their execution and calls the
main-process `CodingAgentManager`. The proxy implementations fail closed if an executor attempts
to run them. All unrelated tools continue through the configured worker executor.

This placement is required for continuation. An isolated worker exits after one tool call, whereas
the manager must retain the coding conversation and workspace so a later `coding_agent_message`
can continue the same session. Delegation is synchronous in this release; no hidden background
work continues after Wyzer returns.

`coding_agent_start` normally requires an existing directory. When the user explicitly requests a
new project at an exact path, its `create_workspace` flag creates that directory before starting the
session. It never guesses a location, treats a typo as permission to create, or accepts a drive root.
Relative paths rooted at a known Windows user folder, such as `Desktop\Projects\Game`, are grounded
to that folder's actual system location; other relative paths are rejected rather than resolved
against Wyzer's process directory.
Follow-up requests such as retry, improve, make usable, run, test, or fix use
`coding_agent_message`; cancellation is reserved for an explicit stop/cancel request.

## Coding loop and tools

The coding agent receives only eight native tools:

- `code_list_directory`
- `code_read_file`
- `code_search`
- `code_write_file`
- `code_edit_file`
- `code_run_command`
- `code_git_status`
- `code_git_diff`

The loop has a hard round limit, one empty-response retry, repeated-call detection, bounded history,
bounded tool results, and a coding-only response budget. Existing files must be read before they can
be replaced or edited. Exact edits support occurrence counts and an optional expected SHA-256;
writes use a same-directory temporary file and atomic replacement.

Commands use argv execution without `shell=True`. Their working directory must resolve inside the
workspace, supplied path arguments cannot traverse outside it, the timeout is capped by coding-agent
configuration, and stdout/stderr are captured in bounded temporary files. An optional bounded
`stdin` value supports smoke-testing interactive command-line programs. Cancellation terminates
the active process tree. This release does not expose commit, push, reset, or publishing tools.

## Workspace containment

The assigned workspace is resolved once when a session starts. Every file path and command working
directory is resolved and checked with `relative_to()` against that root. This rejects `..`, absolute
outside paths, and symlink/junction resolutions that leave the workspace. A missing or ambiguous
workspace is not guessed by Python; the main LLM must supply a grounded directory or ask the user.

Command containment is path- and working-directory-level protection, not a Windows security
sandbox. A permitted compiler or interpreter can itself access operating-system resources. Run
Wyzer with normal user privileges and do not treat coding commands as an AppContainer boundary.

## Evidence and cancellation

Start and message results include session ID, workspace, status, summary, changed files, commands,
and verification. A result is `verified` only when an observed file change is accompanied by a
successful recognized check such as pytest, Ruff, mypy, a build/test command, or Git inspection.
Changes without a meaningful successful check remain `not_verified`; model prose alone never
creates verification.

`coding_agent_status` reads manager state and does not invoke the LLM. `coding_agent_cancel` and
`Orchestrator.interrupt()` cancel the active coding provider request, command process tree, and
remaining coding loop.

## Configuration

```toml
[coding_agent]
enabled = true
maximum_rounds = 12
maximum_history_messages = 40
tool_result_context_characters = 6000
command_timeout_seconds = 60
maximum_output_characters = 12000
max_response_tokens = 1024
```

The coding agent always inherits `[llm]`; there is deliberately no coding model or endpoint setting.
