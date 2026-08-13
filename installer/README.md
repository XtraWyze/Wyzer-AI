# Wyzer Windows installer

Wyzer's installer requires 64-bit Python 3.11. It creates a private virtual environment under
`%LOCALAPPDATA%\Wyzer`; it does not install packages into the PC's global Python environment.

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

Copy `dist\Wyzer-Setup.zip` to the other PC, extract it, and double-click `Install Wyzer.cmd`. The
release includes the current custom avatar frames, wake-word models, and pinned OpenWakeWord
preprocessing models.
The installer downloads the configured Faster-Whisper model. Pass `-SkipModelDownload` only when
preparing an offline installation; voice recognition will not work until the Whisper model is
installed.

The installer automatically detects NVIDIA GPUs and installs the CUDA 12.1 PyTorch build used by
Kokoro TTS. Use `-TorchDevice cpu` to force the smaller CPU build or `-TorchDevice cuda` to require
CUDA and fail installation if it cannot be initialized.

Reinstalling preserves the installed `wyzer.toml`, avatar frames, models, memory, and task state.

An organization-enforced PowerShell restriction, AppLocker rule, or application-control policy
cannot and should not be bypassed by this launcher; an administrator must allow the installer.
