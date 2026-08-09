# Roadmap and completed foundations

Wyzer's typed tools, focused Windows capability packs, managed-browser automation, isolated workers,
local memory, and optional speech loop are complete foundations.

The current architecture is the native tool-calling refactor:

- LLM-first conversation and desktop requests.
- Native Ollama `/api/chat` tools generated from `ToolRegistry` Pydantic schemas.
- Bounded assistant/tool message loop with compact deterministic results.
- Native OpenAI-compatible and llama.cpp function calling.
- Exact-call, expiring, natural yes/no confirmation at consequential boundaries.
- Local cancellation across provider requests, workers, multi-call turns, and confirmations.
- Conservative installed-application fuzzy matching with clarification candidates.

Potential future work should extend registered capabilities without adding a second orchestration
architecture or allowing arbitrary model code execution.
