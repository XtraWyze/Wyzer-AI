# Model context measurements

Measured with `python -m wyzer.dev.measure_context --json` on 2026-08-10. The before values were
captured immediately before capability-scoped views were added. Both schema totals include the
three unchanged task-engine definitions.

| Metric | Before | Default view after | Reduction |
|---|---:|---:|---:|
| Registered tools | 56 | 58 | n/a (two coordination tools added) |
| Registry tools visible to model | 48 | 19 | 29 (60.4%) |
| Task-engine tools visible | 3 | 3 | 0 |
| Serialized tool schemas | 21,065 | 9,322 | 11,743 (55.7%) |
| Approximate schema tokens | 5,266 | 2,330 | 2,936 (55.7%) |
| System prompt + tool schemas | 25,260 | 14,339 | 10,921 (43.2%) |
| Approximate total tokens | 6,315 | 3,585 | 2,730 (43.2%) |

The current registry has 50 available `llm_visible` tools in total, including the two capability
coordination tools. Serializing that complete diagnostic view plus task tools is 21,832 characters.
The default view contains applications, audio, capability coordination, media, system, and windows.
For example, activating `browser` produces 31 registry tools; with the three unchanged task tools,
the serialized schema is 13,634 characters.

No action tool was removed from `ToolRegistry`, and no schema was truncated. Pydantic argument
models, confirmation metadata, hidden status, execution, and evidence policy remain authoritative.
`--activate CAPABILITY` can be repeated to inspect any task-scoped view; the diagnostic is never
included in normal assistant output.
