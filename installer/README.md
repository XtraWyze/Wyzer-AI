# Wyzer Windows installer

The release installer is designed for a fresh 64-bit Windows PC. One double-click installs a
private Python runtime when needed, Wyzer and its speech stack, Ollama, the configured local AI
model, shortcuts, and downloaded speech models. It preserves existing settings and data on
reinstall.

From the working source folder:

```text
Double-click installer\Install Wyzer.cmd
```

This launcher applies `ExecutionPolicy Bypass` only to its child PowerShell process. It does not
change the computer's saved execution policy. Alternatively, run the script directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\install.ps1
```

To make a ZIP for another Windows PC:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build-release.ps1
```

Copy `dist\Wyzer-Setup.zip` to the other PC, extract it, and double-click `Install Wyzer.cmd`.
Nothing has to be installed manually first. The release includes the current custom avatar frames,
wake-word models, and pinned OpenWakeWord preprocessing models. Setup downloads the remaining
dependencies, the configured Faster-Whisper model, and the configured Ollama model, then runs a
readiness check and starts Wyzer.

The default installation requires internet access and several GB of free disk space. It writes a
detailed log to `%LOCALAPPDATA%\Wyzer\install.log` so a failed setup has one obvious diagnostic.
Use `-NoLaunch` to install without starting Wyzer. Advanced/offline packaging can use
`-SkipModelDownload`, `-SkipLlmModelDownload`, or `-SkipLocalAISetup`; the skipped feature will not
be ready until its model or runtime is provided later.

The installer automatically detects NVIDIA GPUs and installs the CUDA 12.1 PyTorch build used by
Kokoro TTS. Use `-TorchDevice cpu` to force the smaller CPU build or `-TorchDevice cuda` to require
CUDA and fail installation if it cannot be initialized.

Reinstalling preserves the installed `wyzer.toml`, avatar frames, models, memory, and task state.
The installer verifies its bundled wake models, installs them into the directory selected by the
preserved configuration, and writes the final diagnostic to
`%LOCALAPPDATA%\Wyzer\install-readiness.json`.

An organization-enforced PowerShell restriction, AppLocker rule, or application-control policy
cannot and should not be bypassed by this launcher; an administrator must allow the installer.
