# Safety and grounding

The model cannot run Python, PowerShell, shell commands, dynamically imported functions, or any
other capability unless a deliberately registered tool provides that exact operation.

Unknown tool names are returned to the model as `UNKNOWN_TOOL`. Invalid Pydantic arguments are
returned as `INVALID_TOOL_ARGUMENTS`, allowing one later model round to correct them without a crash.
Unavailable tools, timeouts, worker crashes, and expected Windows failures are also structured.

## Confirmation policy

Confirmation is trusted tool metadata: `never`, `always`, or `conditional`.

No confirmation is required for routine reversible work, including opening/focusing applications,
files and folders; ordinary browser navigation and searches; window layout changes; volume/media
control; reading desktop or file state; ordinary browser clicks; ordinary typing; and drafting
without submission.

Confirmation is required at the final consequential boundary for sending, submitting, purchasing,
paying, permanent deletion, emptying trash, important overwrite, software install/uninstall,
shutdown/restart/logoff, credentials, and browser controls whose inspected accessible label clearly means one of those actions. Conditional
browser click/type tools enrich confirmation checks from the latest page inspection; the model does
not assign risk.

Wyzer asks a short question such as: `This will send the message to Alex. Should I continue?` The
answers `yes`, `do it`, `go ahead`, `no`, `cancel`, and `never mind` are local controls. Internally,
the pending approval stores the exact validated tool name and arguments, a SHA-256 digest, a step ID,
and an expiration. Changed or expired calls cannot execute. No token is shown to the user.

## Grounding and interruption

Full evidence remains in state and logs. Model context receives a compact deterministic result with
large binary data, hashes, and internal evidence fields removed. Final
wording is produced in the same native tool conversation.

`stop` or `cancel` cancels the active provider request or worker, prevents remaining calls from
starting, clears pending confirmation state, and cannot be overridden by the model.

## Desktop-scene privacy and freshness

The shared desktop scene is updated only by successful on-demand observations. It records which
source produced each fact, its confidence, and its freshness interval. A source that is stale or
missing is not proof of present state; the model is instructed to use a fresh read-only tool when
verification needs it.

Visible lines containing credential, token, payment, or identity keywords are replaced with a
privacy marker before they enter scene context. The scene never stores screenshot bytes, screen
coordinates, browser element references, or UI Automation element IDs.

## Planned-task verification

Planning remains model-driven, but completion is constrained by deterministic state transitions.
A planned step cannot become `verified` until it has a successful read-only observation or a
mutating tool result whose evidence explicitly reports `verified`. Unverified mutations move the
step to `needs_verification`; failures remain visible for bounded retry or revision. Repeated
failures block the task instead of looping indefinitely.

Active task state is written atomically to `.wyzer/task-state.json`. A task that was active when
Wyzer exited is recovered as paused, never silently resumed. `task status`, `pause`, `resume`, and
`stop` are the only deterministic task controls; goal interpretation and recovery choices remain
with the LLM and registered tools.
