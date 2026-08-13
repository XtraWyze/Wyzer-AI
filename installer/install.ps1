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
Copy-NewFiles $wakeSource (Join-Path $InstallRoot "wake-models")

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

$env:WYZER_HOME = $InstallRoot
$env:WYZER_CONFIG = $configTarget
Write-Host "Checking avatars, wake models, speech packages, and Whisper..."
$checkArguments = @("-m", "wyzer.install_check")
if ($SkipModelDownload) {
    $checkArguments += "--allow-missing-model"
} else {
    $checkArguments += "--download-model"
}
& $venvPython @checkArguments
if ($LASTEXITCODE -ne 0) { throw "Wyzer installed, but its readiness check failed. Review the report above." }

Write-Host ""
Write-Host "Wyzer is installed at $InstallRoot"
Write-Host "Use 'Start Wyzer.cmd' or the desktop shortcut to launch it."
Write-Host "Use 'Wyzer Console.cmd' if you need to see startup errors."
