# Model acceptance

Wyzer keeps two read-only model acceptance modes because correctness and preferred tool selection
answer different questions.

`python -m wyzer.model_acceptance` retains the original first-decision contract. It reports whether
the model selected the ideal first tool or expected short sequence. Use it as an efficiency and
tool-selection metric, not as the sole measure of completed-task reliability.

`python -m wyzer.end_to_end_acceptance` runs representative requests for a bounded number of model
rounds. Registered tool calls are schema-validated and receive controlled, case-defined results;
real tools and desktop actions never execute. Capability activation changes the offered schemas
only on the following provider round. Unknown functions, prohibited calls, invalid arguments,
unapproved paths, missing confirmation boundaries, loops, and unsupported completion remain hard
failures.

Each end-to-end case declares its requested goal, allowed successful tool paths, prohibited tools,
required outcome alternatives and argument constraints, model-round limit, clarification policy,
confirmation expectation, mock results, and separate efficiency expectations. Complex planning
cases additionally require a minimum number of distinct steps with explicit success criteria; they
do not require exact plan wording.

The primary metric is end-to-end success rate. Efficiency is reported separately from four equally
weighted measurements: ideal first-tool selection, provider rounds relative to the case ideal, tool
calls relative to the case ideal, and unnecessary observations. Reports also retain raw provider
rounds, calls, activation rounds, latency, validated arguments, confirmation boundaries, and the
complete tool trace so individual scores remain auditable.

The trajectory harness does not test or replace the production executor. Confirmation actions are
never performed: the harness checks the real registered confirmation metadata and records where the
production confirmation boundary would stop execution before returning the controlled result.
Production confirmation sequencing, failure handling, cancellation, isolation, and evidence remain
covered by the deterministic orchestrator tests.
