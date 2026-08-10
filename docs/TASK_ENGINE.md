# LLM-driven task engine

Wyzer uses the task engine only when a request needs two or more distinct computer actions.
Conversation, questions, and single routine actions keep the direct native-tool path.

The primary chat model silently authors outcome-focused steps with success criteria. It uses the
same conversation to act, inspect results, revise an approach, and summarize the outcome. There is
no keyword intent router, command tree, separate planner model, or scripted workflow language.

## State and evidence

Steps move through `pending`, `in_progress`, `needs_verification`, `verified`, `failed`, or
`blocked`. Plans are `active`, `paused`, `completed`, `cancelled`, or `blocked`.

- Successful read-only observations qualify as verification evidence.
- Mutating tools qualify only when their evidence explicitly says the action was verified.
- An unverified mutation requires a relevant observation before step completion.
- While a mutation is awaiting verification, read-only observations remain available but later
  mutations wait so their evidence cannot be attached to the wrong step.
- Repeated failures stop at the configured retry limit.
- Final prose cannot complete an active plan with unfinished steps.

Capability rounds and task-coordination rounds have separate hard bounds. LLM-authored plan
creation, revision, and step transitions therefore cannot consume the action-attempt budget, while
a model that repeats invalid state operations is still stopped safely.

Capability discovery and activation use the coordination bound rather than the computer-action
budget. Activated pack names are stored with an active plan and restored on explicit resume. They
are visibility state only and never satisfy a step's evidence gate.

State is saved atomically at the configured `task_engine.state_path`. Startup changes an
interrupted active plan to paused. A user can inspect or control long work with `task status`,
`pause`, `resume`, and `stop`; all task contents and recovery decisions remain model-driven.

The desktop companion displays compact progress such as `Step 2/4: Confirm the result`. Internal
plans are not narrated in voice unless the user explicitly asks for status.
