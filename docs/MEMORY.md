# Memory

Long-term memory is separate from conversation and desktop state. Records carry provenance,
sensitivity, confidence, and consent state. Memory is never current desktop ground truth.

The local SQLite store requires an explicit command such as `remember that ...`. It rejects
passwords, tokens, private messages, credentials, financial or medical details, full screen text,
and clipboard contents. Use `what do you remember about me`, `forget <topic>`, or
`forget everything you remember about me` to inspect and control the store. Saved facts are
included in bounded Ollama conversation context but are never treated as current desktop truth.

The current conversation buffer is session-only. It keeps a bounded chronological transcript of
user messages, assistant responses, and tool results in memory and is discarded when Wyzer exits.
Wyzer also keeps a separate bounded session-context snapshot of recently observed windows, files,
folders/projects, browser pages/tabs, monitors, actions, and entities. It is derived from successful
tool results and is likewise discarded when Wyzer exits. It is not written to the memory database and
does not create long-term personal memory.
Long-term memories remain in the configured `.wyzer/memory.db` database until explicitly removed.
