# Wyzer Windows installer

Wyzer's installer requires 64-bit Python 3.11. It creates a private virtual environment under
`%LOCALAPPDATA%\Wyzer`; it does not install packages into the PC's global Python environment.

From the working source folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\install.ps1
```

To make a ZIP for another Windows PC:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build-release.ps1
```

Copy `dist\Wyzer-Setup.zip` to the other PC, extract it, and run `install.ps1`. The release includes
the current custom avatar frames and wake-word models. The installer downloads the configured
Faster-Whisper model and OpenWakeWord's required preprocessing models. Pass `-SkipModelDownload`
only when preparing an offline installation; voice recognition will not work until those models
are installed.

Reinstalling preserves the installed `wyzer.toml`, avatar frames, models, memory, and task state.
