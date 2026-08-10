# Local LLM setup

Wyzer requires a chat model with native function/tool calling for desktop actions. Ollama is the
primary provider. Choose a model whose Ollama documentation explicitly lists tool support; a model
that only produces text can still converse but cannot reliably control the desktop.

## Ollama

Start Ollama, pull a tool-capable model, and configure `wyzer.toml`:

```toml
maximum_tool_rounds = 6
confirmation_ttl_seconds = 120
tool_result_context_characters = 4000

[llm]
provider = "ollama"
model = "qwen3:8b"
endpoint = "http://127.0.0.1:11434"
temperature = 0.1
request_timeout_seconds = 60
auto_start = true
startup_timeout_seconds = 10
keep_alive = "30m"
think = false

[perception]
enabled = true
max_image_dimension = 1600
vision_timeout_seconds = 45
visual_click_min_confidence = 0.70
```

Wyzer calls `POST /api/chat` with `stream=false`, a top-level `tools` array, and ordinary
system/user/assistant/tool messages. Tool selection does not use `format` or a structured plan.
`think` stays disabled by default for quick commands. `keep_alive` keeps the model loaded.

The `tools` array is a registry-backed capability view rather than the entire installed surface.
Routine app, window, audio, media, and system commands are present immediately. For browser, file,
clipboard, perception, diagnostics, desktop-interaction, or enabled extension work, the primary
model discovers and activates the relevant pack and receives its tools on a following request.
Natural-language understanding remains entirely model-driven; Wyzer does not select packs from
keywords or deterministic command parsing.

For on-demand screen perception, the configured Ollama model must also support vision. Wyzer
sends the captured JPEG as a base64 `images` entry to `POST /api/chat` only when
`inspect_screen` or `activate_visual_target` is used. Normal text/tool turns remain text-only.

Startup checks `GET /api/tags`. For localhost, `auto_start` can launch `ollama serve` after a
connection failure and retry for the bounded startup timeout.

Run Wyzer:

```powershell
python -m wyzer
```

For Windows Volume Mixer-style controls, install the optional local Core Audio integration:

```powershell
python -m pip install -e ".[audio]"
python -m wyzer.audio_diagnostic
```

Verification sequence:

```text
You: How are you?
You: Open Chrome
You: Move the current window to the other monitor
You: Open Calculator and turn the volume down
You: Open Chrome and move it to monitor two
You: What is on my screen?
You: What does this error message say?
You: stop
```

The first prompt should take one model request. Simple actions should select a tool, execute locally,
and receive a final answer after the result without capability discovery. A first use of a
specialized capability may take discovery and activation rounds. An active planned task retains its
activated packs so they are not repeatedly rediscovered. `stop` is handled locally.

Inspect model-context size without changing normal user output:

```powershell
python -m wyzer.dev.measure_context --json
python -m wyzer.dev.measure_context --activate browser --json
```

The report includes registered and visible tool counts, current/complete serialized schema sizes,
approximate tokens, and activated packs.

## Compatible endpoints

`openai_compatible` uses `/v1/chat/completions` with native `tools` and `tool_calls` and optional
`WYZER_LLM_API_KEY`. `llama_cpp` uses the same native OpenAI-compatible route. If the endpoint or
model does not support function calling, Wyzer reports the provider/tool-call failure; it does not
fall back to fake success or the removed structured-planning architecture.

`provider = "none"` keeps local interruption and memory commands available but clearly reports that
a tool-capable model is not configured for ordinary requests.

Old keys such as `maximum_plan_steps`, `planning_temperature`, and
`maximum_prompt_characters` are rejected rather than silently ignored. Use `maximum_tool_rounds` and
`temperature` instead.
