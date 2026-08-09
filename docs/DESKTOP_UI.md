# Desktop companion UI

The optional desktop UI is deliberately a presentation layer around the existing Wyzer runtime.
It does not add command parsing, intent routing, or a second planner.

## Launch

```powershell
pip install -e ".[ui]"
python -m wyzer --ui --voice
```

Use `--ui --text` to keep wake-word listening off while retaining the character and text chat.

## Interaction

- Drag the character with the left mouse button.
- Click it for a short reaction.
- Double-click it to open the small text chat window.
- Right-click it for Chat, Listen now, Stop current task, Mute voice, Ambient comments, Hide, and Quit.
- Stop current task now also cuts off active TTS. Typing `stop` or `cancel` in the chat uses the
  same global interrupt path. In voice mode, `hey wyzer`, `wyzer stop`, or `wyzer cancel` can barge
  in while Wyzer is speaking.
- Wyzer responses and listening/thinking states appear in speech bubbles.
- Ambient comments are local UI flavor only; they never invoke tools or inject fake user turns into the LLM conversation.

## Custom character art

The UI ships with a lightweight vector placeholder so the feature runs without bundled artwork.
For a custom character, create `.wyzer/avatar/` and add transparent frames such as:

```text
.wyzer/avatar/
  idle1.png
  idle2.png
  walk1.png
  drag1.png
  fall1.png
  sit1.png
```

The same prefixes support numbered PNG or WebP frames. Frames are loaded in numeric filename order
and scaled to the character window. Missing behavior art falls back to the idle animation. Removing
the files restores the built-in placeholder.

## Architecture

```text
PySide6 character/chat
        |
        v
AssistantRuntime (one background asyncio loop)
        |
        v
existing Orchestrator
        |
        v
Qwen native tool calling
        |
        v
existing Wyzer tool registry/executor
```

Voice wake-word handling and typed UI requests use the same asyncio loop and the same Orchestrator,
so conversation state, confirmations, interruption, memory, and tool results remain shared.
