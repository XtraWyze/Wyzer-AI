"""Interactive terminal interface for the text-first assistant."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import deque
from collections.abc import Sequence
from functools import partial

from wyzer.app.orchestrator import Orchestrator
from wyzer.brain import ChatProvider, create_chat_provider, diagnostic_provider
from wyzer.config import WyzerSettings
from wyzer.events import EventLedger
from wyzer.memory import MemoryStore
from wyzer.policy import ConfirmationPolicy
from wyzer.runtime_paths import configure_runtime_paths, data_home, find_config_path
from wyzer.speech import (
    FasterWhisperRecognizer,
    OpenWakeWordDetector,
    WindowsPhraseDetector,
    WindowsSpeechRecognizer,
    WindowsWakeWordDetector,
    create_speech_synthesizer,
    find_wake_model,
)
from wyzer.speech.text import normalize_spoken_command, speech_safe_text
from wyzer.speech.windows import SpeechAdapterError
from wyzer.state import WorldStateManager
from wyzer.tasks import TaskStateStore
from wyzer.tools import create_default_registry, discover_tool_pack_names
from wyzer.tools.builtin_packs import BUILTIN_PACK_NAMES
from wyzer.workers import InProcessExecutor, IsolatedExecutor


def build_assistant(settings: WyzerSettings, provider: ChatProvider | None = None) -> Orchestrator:
    registry_factory = partial(
        create_default_registry,
        audio_options=settings.audio.model_dump(),
        perception_options={
            **settings.perception.model_dump(),
            "provider": settings.llm.provider,
            "endpoint": str(settings.llm.endpoint).rstrip("/")
            if settings.llm.endpoint
            else "http://127.0.0.1:11434",
            "model": settings.llm.model,
            "temperature": 0.0,
            "think": False,
            "keep_alive": settings.llm.keep_alive,
        },
        enabled_entrypoint_packs=tuple(settings.tool_packs.enabled),
    )
    registry = registry_factory()
    executor = (
        IsolatedExecutor(
            registry_factory,
            maximum_workers=settings.tool_worker_count,
            default_timeout_seconds=settings.tool_timeout_seconds,
            tool_timeouts={
                name: registry.get(name, require_available=False).default_timeout_seconds
                for name in registry
            },
            ipc_directory=data_home() / "worker-ipc",
        )
        if settings.worker_isolation_enabled
        else InProcessExecutor(registry)
    )
    return Orchestrator(
        registry,
        executor,
        provider or create_chat_provider(settings.llm, settings.personality),
        maximum_tool_rounds=settings.maximum_tool_rounds,
        tool_result_context_characters=settings.tool_result_context_characters,
        ledger=EventLedger(settings.event_ledger_size),
        world=WorldStateManager(),
        confirmation_policy=ConfirmationPolicy(settings.confirmation_ttl_seconds),
        memory=(MemoryStore(settings.memory.database_path) if settings.memory.enabled else None),
        tasks=(
            TaskStateStore(
                settings.task_engine.state_path,
                maximum_steps=settings.task_engine.maximum_steps,
                maximum_retries_per_step=settings.task_engine.maximum_retries_per_step,
            )
            if settings.task_engine.enabled
            else None
        ),
        personality=settings.personality.model_dump(),
        detailed_output_tokens=settings.llm.detailed_output_tokens,
    )


async def chat(assistant: Orchestrator, assistant_name: str) -> None:
    print(f"{assistant_name} is ready. Type 'help' for commands or 'quit' to exit.")
    loop = asyncio.get_running_loop()
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    reader = asyncio.create_task(asyncio.to_thread(_console_reader, loop, inputs))
    pending: deque[str] = deque()
    while True:
        text = pending.popleft() if pending else await inputs.get()
        if text is None:
            break
        if text.strip().casefold() in {"quit", "exit"}:
            break
        if not text.strip():
            continue
        action = asyncio.create_task(assistant.handle(text))
        while True:
            next_input = asyncio.create_task(inputs.get())
            done, _ = await asyncio.wait({action, next_input}, return_when=asyncio.FIRST_COMPLETED)
            if action in done:
                response = action.result()
                print(console_safe_text(f"{assistant_name}: {response.text}"))
                if next_input in done:
                    queued = next_input.result()
                    if queued is not None:
                        pending.append(queued)
                else:
                    next_input.cancel()
                break
            queued = next_input.result()
            if queued is None:
                assistant.interrupt()
                await action
                reader.cancel()
                return
            if re.fullmatch(
                r"\s*(?:stop|cancel|interrupt|never mind|pause(?:\s+task)?)\s*[.!]?\s*",
                queued,
                re.I,
            ):
                interrupted = await assistant.handle(queued)
                print(console_safe_text(f"{assistant_name}: {interrupted.text}"))
                await action
                break
            pending.append(queued)
    if not reader.done():
        reader.cancel()


def _console_reader(loop: asyncio.AbstractEventLoop, inputs: asyncio.Queue[str | None]) -> None:
    while True:
        try:
            text = input("You: ")
        except (EOFError, KeyboardInterrupt):
            loop.call_soon_threadsafe(inputs.put_nowait, None)
            return
        loop.call_soon_threadsafe(inputs.put_nowait, text)
        if text.strip().casefold() in {"quit", "exit"}:
            return


def console_safe_text(text: str, encoding: str | None = None) -> str:
    """Make arbitrary model text printable by the active Windows console encoding."""
    text = speech_safe_text(text)
    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(target_encoding, errors="replace").decode(
            target_encoding, errors="replace"
        )
    except LookupError:
        return text.encode("utf-8", errors="replace").decode("utf-8")


def _voice_interrupt_phrases(wake_phrase: str, *, allow_bare: bool) -> tuple[str, ...]:
    wake = " ".join(wake_phrase.strip().split())
    phrases = [
        f"{wake} stop",
        f"{wake} cancel",
        "wyzer stop",
        "wyzer cancel",
        "stop wyzer",
        "cancel wyzer",
    ]
    if allow_bare:
        phrases.extend(
            (
                f"{wake} pause",
                "wyzer pause",
                "pause wyzer",
                "stop",
                "cancel",
                "pause",
                "interrupt",
                "never mind",
            )
        )
    else:
        phrases.insert(0, wake)
    return tuple(dict.fromkeys(phrase for phrase in phrases if phrase))


async def _wait_for_voice_interrupt(
    detector: WindowsPhraseDetector,
) -> str | None:
    try:
        recognized = await asyncio.to_thread(detector.recognize, 300.0)
        return recognized.text if recognized is not None else None
    except SpeechAdapterError:
        return None


async def _handle_voice_request_interruptibly(
    assistant: Orchestrator,
    text: str,
    wake_phrase: str,
):
    action = asyncio.create_task(assistant.handle(text))
    detector = WindowsPhraseDetector(_voice_interrupt_phrases(wake_phrase, allow_bare=True))
    interrupt_task = asyncio.create_task(_wait_for_voice_interrupt(detector))
    interrupted = False
    try:
        done, _ = await asyncio.wait({action, interrupt_task}, return_when=asyncio.FIRST_COMPLETED)
        control = interrupt_task.result() if interrupt_task in done else None
        if control is not None:
            interrupted = True
            if re.search(r"\bpause\b", control, re.I):
                paused = await assistant.handle("pause")
                if paused.interrupted:
                    await action
                    return paused, interrupted
                interrupted = False
                return await action, interrupted
            assistant.interrupt()
        response = await action
        return response, interrupted
    finally:
        detector.cancel()
        if not interrupt_task.done():
            await interrupt_task


async def _speak_voice_reply_interruptibly(
    speaker,
    text: str,
    wake_phrase: str,
) -> bool:
    detector = WindowsPhraseDetector(_voice_interrupt_phrases(wake_phrase, allow_bare=False))
    interrupt_task = asyncio.create_task(_wait_for_voice_interrupt(detector))
    speak_task = asyncio.create_task(asyncio.to_thread(speaker.speak, text))
    interrupted = False
    try:
        done, _ = await asyncio.wait(
            {speak_task, interrupt_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if interrupt_task in done and interrupt_task.result() is not None:
            interrupted = True
            speaker.stop()
        await speak_task
        return interrupted
    finally:
        detector.cancel()
        if not interrupt_task.done():
            await interrupt_task


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Wyzer local Windows assistant")
    parser.add_argument("--voice", action="store_true", help="use wake-word speech mode")
    parser.add_argument("--text", action="store_true", help="force terminal text mode")
    parser.add_argument(
        "--ui",
        action="store_true",
        help="show the optional desktop character UI (install with pip install -e .[ui])",
    )
    parser.add_argument("--wake-word", help="override the configured wake phrase")
    parser.add_argument(
        "--list-tool-packs",
        action="store_true",
        help="list built-in, installed, and enabled tool packs, then exit",
    )
    arguments = parser.parse_args(argv)
    config_path = find_config_path()
    settings = configure_runtime_paths(WyzerSettings.load(config_path), config_path)
    if arguments.list_tool_packs:
        installed = discover_tool_pack_names()
        print("Built-in tool packs: " + ", ".join(BUILTIN_PACK_NAMES))
        print("Installed external tool packs: " + (", ".join(installed) or "none"))
        print("Enabled external tool packs: " + (", ".join(settings.tool_packs.enabled) or "none"))
        return
    voice_enabled = (settings.speech.enabled or arguments.voice) and not arguments.text
    wake_phrase = arguments.wake_word or settings.speech.wake_phrase

    if arguments.ui:
        # Qt must own the main Windows thread before pywinauto/comtypes are
        # constructed by the tool registry.  pywinauto otherwise defaults the
        # thread to MTA, while Qt/OLE requires STA, producing 0x80010106.
        # Tell comtypes/pywinauto to use the same apartment model as Qt.
        if sys.platform == "win32":
            sys.coinit_flags = 2  # COINIT_APARTMENTTHREADED
        try:
            from PySide6.QtWidgets import QApplication

            from wyzer.ui import run_desktop_ui
        except ImportError as error:
            if error.name and error.name.startswith("PySide6"):
                raise SystemExit(
                    'Wyzer desktop UI requires PySide6. Install it with: pip install -e ".[ui]"'
                ) from error
            raise

        # Construct QApplication before the assistant/tool registry so Qt sets
        # process DPI awareness and OLE state before Windows automation libraries.
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        provider = create_chat_provider(settings.llm, settings.personality)
        assistant = build_assistant(settings, provider)
        run_desktop_ui(
            assistant,
            provider,
            settings,
            app=app,
            voice_enabled=voice_enabled,
            wake_phrase=wake_phrase,
        )
        return

    provider = create_chat_provider(settings.llm, settings.personality)
    assistant = build_assistant(settings, provider)
    asyncio.run(_start(assistant, provider, settings, voice_enabled, wake_phrase))


async def _start(
    assistant: Orchestrator,
    provider: ChatProvider,
    settings: WyzerSettings,
    voice_enabled: bool = False,
    wake_phrase: str | None = None,
) -> None:
    assistant.world.set_operating_mode("voice" if voice_enabled else "text")
    diagnosable = diagnostic_provider(provider)
    if diagnosable is not None:
        diagnostic = await diagnosable.diagnose()
        status = "ready" if diagnostic.available else "unavailable"
        print(f"Local model ({diagnostic.provider}): {status} - {diagnostic.message}")
        if voice_enabled and diagnostic.available:
            print(f"Local model ({diagnostic.provider}): loading into memory...")
            try:
                await diagnosable.warm_up()
            except Exception as error:
                print(f"Local model ({diagnostic.provider}): warm-up failed - {error}")
            else:
                print(f"Local model ({diagnostic.provider}): loaded and ready.")
    elif settings.llm.provider == "none":
        print("Local model: disabled; local stop and memory commands remain available.")
    if voice_enabled:
        await voice_chat(
            assistant,
            settings.personality.assistant_name,
            settings,
            wake_phrase or settings.speech.wake_phrase,
        )
    else:
        await chat(assistant, settings.personality.assistant_name)


async def voice_chat(
    assistant: Orchestrator,
    assistant_name: str,
    settings: WyzerSettings,
    wake_phrase: str,
) -> None:
    microphone = WindowsSpeechRecognizer(minimum_confidence=settings.speech.minimum_stt_confidence)
    if settings.speech.stt_adapter == "faster_whisper":
        recognizer: FasterWhisperRecognizer | WindowsSpeechRecognizer = FasterWhisperRecognizer(
            settings.speech.whisper_model,
            device=settings.speech.whisper_device,
            compute_type=settings.speech.whisper_compute_type,
            download_root=settings.speech.whisper_download_root,
            minimum_confidence=settings.speech.minimum_stt_confidence,
            capture_utterance=microphone.capture_utterance,
        )
    else:
        recognizer = microphone
    try:
        if settings.speech.wake_word_adapter == "openwakeword":
            model_path = find_wake_model(
                settings.speech.wake_model_directory, settings.speech.wake_model
            )
            wake: OpenWakeWordDetector | WindowsWakeWordDetector = OpenWakeWordDetector(
                model_path, threshold=settings.speech.minimum_wake_confidence
            )
            wake_description = f"{wake_phrase}' ({model_path.name})"
        else:
            wake = WindowsWakeWordDetector(
                wake_phrase,
                minimum_confidence=settings.speech.minimum_wake_confidence,
            )
            wake_description = wake_phrase + "'"
    except SpeechAdapterError as error:
        print(f"Speech: unavailable - {error}")
        print("Falling back to terminal chat.")
        await chat(assistant, assistant_name)
        return
    try:
        speaker = create_speech_synthesizer(settings.speech)
        print(f"Speech output ({settings.speech.tts_adapter}): loading...")
        await asyncio.to_thread(speaker.warm_up)
        print(f"Speech output ({settings.speech.tts_adapter}): loaded and ready.")
    except SpeechAdapterError as error:
        print(f"Speech: unavailable - {error}")
        print("Falling back to terminal chat.")
        await chat(assistant, assistant_name)
        return
    diagnostic = await asyncio.to_thread(recognizer.diagnose)
    if not diagnostic.available:
        print(f"Speech: unavailable - {diagnostic.message}")
        print("Falling back to terminal chat.")
        await chat(assistant, assistant_name)
        return
    print(f"Speech: ready. Say '{wake_description} to wake {assistant_name}; say 'quit' to exit.")
    while True:
        try:
            detected = await asyncio.to_thread(wake.wait, settings.speech.wake_timeout_seconds)
            if not detected:
                continue
            print(f"{assistant_name}: Listening...")
            heard = await asyncio.to_thread(
                recognizer.listen, settings.speech.listen_timeout_seconds
            )
            if heard is None:
                print(f"{assistant_name}: I didn't catch that.")
                await asyncio.to_thread(speaker.speak, "I didn't catch that.")
                continue
            print(console_safe_text(f"You: {heard.text}"))
            if heard.text.strip().casefold() in {"quit", "exit", "goodbye"}:
                await asyncio.to_thread(speaker.speak, "Goodbye.")
                break
            response, interrupted = await _handle_voice_request_interruptibly(
                assistant,
                normalize_spoken_command(heard.text),
                wake_phrase,
            )
            print(console_safe_text(f"{assistant_name}: {response.text}"))
            if interrupted:
                continue
            await _speak_voice_reply_interruptibly(
                speaker,
                speech_safe_text(response.text),
                wake_phrase,
            )
        except SpeechAdapterError as error:
            print(f"Speech error: {error}")
        except (EOFError, KeyboardInterrupt):
            print()
            break


if __name__ == "__main__":
    main()
