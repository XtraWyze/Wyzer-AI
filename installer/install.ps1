[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Wyzer"),
    [switch]$SkipModelDownload,
    [switch]$NoShortcut,
    [ValidateSet("auto", "cuda", "cpu")]
    [string]$TorchDevice = "auto"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Find-Python311 {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        & $launcher.Source -3.11 -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Executable = $launcher.Source; Prefix = @("-3.11") }
        }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        & $python.Source -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Executable = $python.Source; Prefix = @() }
        }
    }
    throw "Wyzer requires 64-bit Python 3.11. Install it from python.org, then rerun this installer."
}

function Copy-NewFiles([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source)) { return }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -File | ForEach-Object {
        $target = Join-Path $Destination $_.Name
        if (-not (Test-Path -LiteralPath $target)) {
            Copy-Item -LiteralPath $_.FullName -Destination $target
        }
    }
}

function Install-WakeWordModels(
    [string]$Source,
    [string[]]$Destinations
) {
    $expectedHashes = @{
        "hey_Wyzer.onnx" = "DFCADF0902C52F230E59D671D4AD6FC86A3E7116FCF751BB4818334F54539700"
        "hey_wiser.onnx" = "1B44C4161528E158CEF225DA38167120317CA093F5A1F5BA4C0BFB391EA08591"
    }
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Bundled wake-word model directory is missing: $Source"
    }
    foreach ($name in $expectedHashes.Keys) {
        $sourceModel = Join-Path $Source $name
        if (-not (Test-Path -LiteralPath $sourceModel)) {
            throw "Bundled wake-word model is missing: $name"
        }
        $sourceHash = (Get-FileHash -LiteralPath $sourceModel -Algorithm SHA256).Hash
        if ($sourceHash -ne $expectedHashes[$name]) {
            throw "Bundled wake-word model failed its SHA-256 check: $name"
        }
    }

    foreach ($destination in @($Destinations | Where-Object { $_ } | Sort-Object -Unique)) {
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        foreach ($name in $expectedHashes.Keys) {
            $sourceModel = Join-Path $Source $name
            $target = Join-Path $destination $name
            Copy-Item -LiteralPath $sourceModel -Destination $target -Force
            $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
            if ($targetHash -ne $expectedHashes[$name]) {
                throw "Installed wake-word model failed verification: $target"
            }
        }
        $installedModels = @(Get-ChildItem -LiteralPath $destination -Filter "*.onnx" -File)
        if ($installedModels.Count -eq 0) {
            throw "No wake-word ONNX model was installed in $destination"
        }
        Write-Host "Wake-word models ready in $destination"
    }
}

function Install-OpenWakeWordSupportModels(
    [string]$Source,
    [string]$PythonExecutable
) {
    if (-not (Test-Path -LiteralPath $Source)) { return }
    $expectedHashes = @{
        "melspectrogram.onnx" = "BA2B0E0F8B7B875369A2C89CB13360FF53BAC436F2895CCED9F479FA65EB176F"
        "embedding_model.onnx" = "70D164290C1D095D1D4EE149BC5E00543250A7316B59F31D056CFF7BD3075C1F"
    }
    $installedRoot = & $PythonExecutable -c "from pathlib import Path; import openwakeword; print(Path(openwakeword.__file__).resolve().parent / 'resources' / 'models')" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $installedRoot) {
        throw "Could not locate OpenWakeWord's installed support-model directory."
    }
    $destination = [string]($installedRoot | Select-Object -Last 1)
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    foreach ($name in $expectedHashes.Keys) {
        $sourceModel = Join-Path $Source $name
        if (-not (Test-Path -LiteralPath $sourceModel)) {
            throw "Bundled OpenWakeWord support model is missing: $name"
        }
        $sourceHash = (Get-FileHash -LiteralPath $sourceModel -Algorithm SHA256).Hash
        if ($sourceHash -ne $expectedHashes[$name]) {
            throw "Bundled OpenWakeWord support model failed its SHA-256 check: $name"
        }
        $target = Join-Path $destination $name
        Copy-Item -LiteralPath $sourceModel -Destination $target -Force
        $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($targetHash -ne $expectedHashes[$name]) {
            throw "Installed OpenWakeWord support model failed verification: $name"
        }
    }
}

function Set-ShortcutRunAsAdministrator([string]$ShortcutPath) {
    $shortcutBytes = [System.IO.File]::ReadAllBytes($ShortcutPath)
    if ($shortcutBytes.Length -le 0x15) {
        throw "The desktop shortcut could not be marked to run as administrator."
    }
    # Set the Shell Link SLDF_RUNAS_USER flag (0x00002000) in LinkFlags.
    $shortcutBytes[0x15] = [byte]($shortcutBytes[0x15] -bor 0x20)
    [System.IO.File]::WriteAllBytes($ShortcutPath, $shortcutBytes)
}

function Test-NvidiaGpu {
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($null -eq $nvidiaSmi) { return $false }
    & $nvidiaSmi.Source --query-gpu=name --format=csv,noheader 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Test-CudaTorch([string]$PythonExecutable) {
    & $PythonExecutable -c "import torch; raise SystemExit(0 if torch.version.cuda and torch.cuda.is_available() else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

$scriptDirectory = $PSScriptRoot
$sourceRoot = Split-Path -Parent $scriptDirectory
$constraints = Join-Path $scriptDirectory "constraints-windows-py311.txt"
$releaseWheel = Get-ChildItem -LiteralPath $scriptDirectory -Filter "wyzer-*.whl" -File -ErrorAction SilentlyContinue | Select-Object -First 1
$packageSource = if ($null -ne $releaseWheel) { $releaseWheel.FullName } else { $sourceRoot }
$avatarSource = if (Test-Path (Join-Path $scriptDirectory "assets\avatar")) {
    Join-Path $scriptDirectory "assets\avatar"
} else {
    Join-Path $sourceRoot ".wyzer\avatar"
}
$wakeSource = if (Test-Path (Join-Path $scriptDirectory "assets\wake-models")) {
    Join-Path $scriptDirectory "assets\wake-models"
} else {
    Join-Path $sourceRoot "openwakemodels"
}
$configSource = if (Test-Path (Join-Path $scriptDirectory "wyzer.toml")) {
    Join-Path $scriptDirectory "wyzer.toml"
} else {
    Join-Path $sourceRoot "wyzer.toml"
}

if (-not (Test-Path -LiteralPath $constraints)) {
    throw "Installer constraints file is missing: $constraints"
}
if (-not (Test-Path -LiteralPath $packageSource)) {
    throw "Wyzer package source is missing: $packageSource"
}
if (-not (Test-Path -LiteralPath $wakeSource)) {
    throw "Wake-word model assets are missing: $wakeSource"
}
if (@(Get-ChildItem -LiteralPath $wakeSource -Filter "*.onnx" -File).Count -eq 0) {
    throw "The setup package contains no wake-word ONNX models: $wakeSource"
}

$python = Find-Python311
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$venv = Join-Path $InstallRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating Wyzer's private Python environment..."
    & $python.Executable @($python.Prefix) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Wyzer virtual environment." }
}

Write-Host "Installing the tested Wyzer dependency set..."
& $venvPython -m pip install --upgrade pip wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update the installer tools." }
& $venvPython -m pip install --constraint $constraints "$packageSource[audio,ui]"
if ($LASTEXITCODE -ne 0) { throw "Wyzer or one of its dependencies could not be installed." }

Install-OpenWakeWordSupportModels `
    (Join-Path $scriptDirectory "assets\openwakeword-support") `
    $venvPython

$cudaRequested = $TorchDevice -eq "cuda" -or ($TorchDevice -eq "auto" -and (Test-NvidiaGpu))
if ($cudaRequested) {
    if (-not (Test-CudaTorch $venvPython)) {
        Write-Host "NVIDIA GPU detected. Installing CUDA-enabled PyTorch..."
        & $venvPython -m pip install --upgrade --force-reinstall "torch==2.3.1" --index-url "https://download.pytorch.org/whl/cu121"
        if ($LASTEXITCODE -ne 0) { throw "Could not install CUDA-enabled PyTorch." }
        if (-not (Test-CudaTorch $venvPython)) {
            throw "CUDA-enabled PyTorch was installed, but CUDA is still unavailable. Check the NVIDIA driver."
        }
    }
    Write-Host "CUDA-enabled PyTorch is ready."
} elseif ($TorchDevice -eq "auto") {
    Write-Host "No NVIDIA GPU was detected. Keeping the CPU PyTorch build."
} elseif (Test-CudaTorch $venvPython) {
    Write-Host "Replacing CUDA-enabled PyTorch with the requested CPU build..."
    & $venvPython -m pip install --upgrade --force-reinstall "torch==2.3.1" --index-url "https://download.pytorch.org/whl/cpu"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the CPU PyTorch build." }
}

$configTarget = Join-Path $InstallRoot "wyzer.toml"
if (-not (Test-Path -LiteralPath $configTarget)) {
    if (-not (Test-Path -LiteralPath $configSource)) { throw "Default wyzer.toml is missing." }
    $configText = [System.IO.File]::ReadAllText($configSource)
    $configText = $configText.Replace('state_path = ".wyzer/task-state.json"', 'state_path = "task-state.json"')
    $configText = $configText.Replace('database_path = ".wyzer/memory.db"', 'database_path = "memory.db"')
    $configText = $configText.Replace('wake_model_directory = "openwakemodels"', 'wake_model_directory = "wake-models"')
    $configText = $configText.Replace('whisper_download_root = ".wyzer/models"', 'whisper_download_root = "models"')
    $configText = $configText.Replace('tts_device = "cuda"', 'tts_device = "auto"')
    [System.IO.File]::WriteAllText($configTarget, $configText, [System.Text.UTF8Encoding]::new($false))
}

Copy-NewFiles $avatarSource (Join-Path $InstallRoot "avatar")

$env:WYZER_HOME = $InstallRoot
$env:WYZER_CONFIG = $configTarget
$configuredWakeDirectoryOutput = & $venvPython -c "from wyzer.config import WyzerSettings; from wyzer.runtime_paths import configure_runtime_paths, find_config_path; p = find_config_path(); s = configure_runtime_paths(WyzerSettings.load(p), p); print(s.speech.wake_model_directory)" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $configuredWakeDirectoryOutput) {
    throw "Could not resolve the wake-word model directory from $configTarget"
}
$configuredWakeDirectory = [string]($configuredWakeDirectoryOutput | Select-Object -Last 1)
Install-WakeWordModels $wakeSource @(
    (Join-Path $InstallRoot "wake-models"),
    $configuredWakeDirectory
)

$launcherPath = Join-Path $InstallRoot "Start Wyzer.cmd"
$hiddenLauncherPath = Join-Path $InstallRoot "Start Wyzer.vbs"
$consolePath = Join-Path $InstallRoot "Wyzer Console.cmd"
$launcherText = @"
@echo off
set "WYZER_HOME=$InstallRoot"
set "WYZER_CONFIG=$configTarget"
"$venvPython" -m wyzer --ui --voice
"@
$hiddenLauncherText = @"
Set shell = CreateObject("WScript.Shell")
shell.Environment("PROCESS")("WYZER_HOME") = "$InstallRoot"
shell.Environment("PROCESS")("WYZER_CONFIG") = "$configTarget"
shell.Run """$venvPython"" -m wyzer --ui --voice", 0, False
"@
$consoleText = @"
@echo off
set "WYZER_HOME=$InstallRoot"
set "WYZER_CONFIG=$configTarget"
"$venvPython" -m wyzer --ui --voice
pause
"@
[System.IO.File]::WriteAllText($launcherPath, $launcherText, [System.Text.ASCIIEncoding]::new())
[System.IO.File]::WriteAllText($hiddenLauncherPath, $hiddenLauncherText, [System.Text.ASCIIEncoding]::new())
[System.IO.File]::WriteAllText($consolePath, $consoleText, [System.Text.ASCIIEncoding]::new())

if (-not $NoShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Wyzer.lnk"
    $wscriptPath = Join-Path $env:WINDIR "System32\wscript.exe"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $wscriptPath
    $shortcut.Arguments = "`"$hiddenLauncherPath`""
    $shortcut.WorkingDirectory = $InstallRoot
    $shortcut.Description = "Start Wyzer as administrator"
    $shortcut.Save()
    Set-ShortcutRunAsAdministrator $shortcutPath
}

Write-Host "Checking avatars, wake models, speech packages, and Whisper..."
$checkArguments = @("-m", "wyzer.install_check")
if ($SkipModelDownload) {
    $checkArguments += "--allow-missing-model"
} else {
    $checkArguments += "--download-model"
}
$readinessReport = Join-Path $InstallRoot "install-readiness.json"
$checkArguments += @("--output", $readinessReport)
& $venvPython @checkArguments
if ($LASTEXITCODE -ne 0) {
    throw "Wyzer installed, but its readiness check failed. The report was saved to $readinessReport"
}

Write-Host ""
Write-Host "Wyzer is installed at $InstallRoot"
Write-Host "Use 'Start Wyzer.cmd' or the desktop shortcut to launch it."
Write-Host "Use 'Wyzer Console.cmd' if you need to see startup errors."
