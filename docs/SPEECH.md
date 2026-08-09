# Speech mode

Wyzer can run an opt-in, fully local voice loop on Windows:

```powershell
python -m wyzer --voice
```

The configured OpenWakeWord ONNX model listens for the wake phrase. After detection,
Faster-Whisper transcribes one utterance locally, Wyzer sends ordinary requests through the same
native tool-calling loop as text mode, and the configured local Kokoro voice speaks the grounded
response. Set `tts_adapter = "windows_system"` to use an installed SAPI voice instead.
Say `quit`, `exit`, or `goodbye` after waking Wyzer to leave voice mode. `Ctrl+C` also exits.

After saying the wake phrase, wait for `Wyzer: Listening...`, speak normally, and then pause. Wyzer
captures microphone audio immediately, detects the ending silence, transcribes the temporary WAV
locally, and deletes it before handling the command.

The system context marks voice mode, so responses remain short and speakable. A final speech
sanitizer removes pictographs if a model
ignores that instruction, and console output is normalized so arbitrary model Unicode cannot crash
older Windows code pages. Natural state questions and follow-ups are grounded directly, including
`what's open right now`, `what windows are on monitor one`, `what about monitor two`,
`which one is focused`, and `what do you know about my computer`.

The default configuration uses `openwakemodels/hey_Wyzer.onnx`. Wake models remain local and audio
is processed in memory; Wyzer does not retain microphone recordings. Windows microphone privacy
permissions must allow desktop applications. A Windows speech-recognition language is required for
the Windows fallback recognizer. A SAPI voice is required only when `tts_adapter` is
`windows_system`.

Relevant `wyzer.toml` settings:

```toml
[speech]
enabled = false
stt_adapter = "faster_whisper"
wake_word_adapter = "openwakeword"
wake_model_directory = "openwakemodels"
wake_model = "hey_Wyzer.onnx"
wake_phrase = "hey wyzer"
minimum_wake_confidence = 0.55
minimum_stt_confidence = 0.35
whisper_model = "small.en"
whisper_device = "cpu"
whisper_compute_type = "int8"
whisper_download_root = ".wyzer/models"
listen_timeout_seconds = 8
wake_timeout_seconds = 30
rate = 0
volume = 100
```

Set `enabled = true` to make voice mode the default, or keep it false and use `--voice`. Use
`--text` to force terminal mode. The `wake_phrase` setting is the user-facing phrase description;
the acoustic phrase itself is determined by the selected ONNX model.

`whisper_device = "auto"` prefers CUDA and falls back to CPU `int8` if the NVIDIA cuBLAS/cuDNN
runtime is unavailable. Voice-activity and Whisper no-speech checks reject silent captures instead
of turning common silence hallucinations into commands.

After waking Wyzer, `stop`, `cancel`, and `never mind` interrupt the active tool loop or pending
confirmation locally. While a task is still running, the same short control phrases are monitored
on a separate local grammar so the normal request loop does not have to finish first. While Wyzer
is speaking, say `hey wyzer`, `wyzer stop`, `wyzer cancel`, or the equivalent wake-phrase form to
cut off TTS immediately. The wake/name prefix is required during TTS to prevent Wyzer from hearing
the word "stop" in its own response and cancelling itself. Consequential actions use spoken yes/no
confirmation; no token is read aloud.
