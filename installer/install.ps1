[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Wyzer"),
    [switch]$SkipModelDownload,
    [switch]$SkipLocalAISetup,
    [switch]$SkipLlmModelDownload,
    [switch]$NoShortcut,
    [switch]$NoLaunch,
    [ValidateSet("auto", "cuda", "cpu")]
    [string]$TorchDevice = "auto"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Wyzer requires a 64-bit edition of Windows."
}
$windowsInfo = Get-ItemProperty `
    "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" `
    -ErrorAction Stop
$windowsBuild = [int]$windowsInfo.CurrentBuildNumber
if ($windowsBuild -lt 19045) {
    throw "Wyzer requires Windows 10 22H2 or newer. This PC reports Windows build $windowsBuild."
}

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$installLog = Join-Path $InstallRoot "install.log"
try {
    Start-Transcript -LiteralPath $installLog -Append | Out-Null
} catch {
    Write-Warning "The detailed install log could not be started: $($_.Exception.Message)"
}

function Write-InstallStep([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Python311(
    [string]$Executable,
    [string[]]$Prefix = @()
) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    & $Executable @Prefix -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8 else 1)" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Find-Python311 {
    $privatePython = Join-Path $InstallRoot "Python311\python.exe"
    if (Test-Python311 $privatePython) {
        return [PSCustomObject]@{ Executable = $privatePython; Prefix = @() }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $launcher -and (Test-Python311 $launcher.Source @("-3.11"))) {
        return [PSCustomObject]@{ Executable = $launcher.Source; Prefix = @("-3.11") }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python -and $python.Source -notlike "*\WindowsApps\*") {
        if (Test-Python311 $python.Source) {
            return [PSCustomObject]@{ Executable = $python.Source; Prefix = @() }
        }
    }

    $knownLocations = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe")
    )
    foreach ($candidate in $knownLocations) {
        if (Test-Python311 $candidate) {
            return [PSCustomObject]@{ Executable = $candidate; Prefix = @() }
        }
    }
    return $null
}

function Assert-TrustedInstaller(
    [string]$Path,
    [string[]]$ExpectedPublishers
) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "The downloaded installer did not have a valid Windows signature: $Path"
    }
    $subject = [string]$signature.SignerCertificate.Subject
    if (-not @($ExpectedPublishers | Where-Object { $subject -like "*$_*" })) {
        throw "The downloaded installer was signed by an unexpected publisher: $subject"
    }
}

function Save-TrustedDownload(
    [string]$Uri,
    [string]$Destination,
    [string[]]$ExpectedPublishers
) {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination
    Assert-TrustedInstaller $Destination $ExpectedPublishers
}

function Install-PrivatePython311 {
    $target = Join-Path $InstallRoot "Python311"
    $installer = Join-Path ([IO.Path]::GetTempPath()) "wyzer-python-3.11.9-amd64.exe"
    Write-InstallStep "Installing Wyzer's private Python runtime"
    try {
        Save-TrustedDownload `
            "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" `
            $installer `
            @("Python Software Foundation")
        $arguments = '/quiet InstallAllUsers=0 TargetDir="' + $target + '" Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 AssociateFiles=0 Shortcuts=0 PrependPath=0 Include_pip=1'
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "The Python installer returned exit code $($process.ExitCode)."
        }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    $python = Find-Python311
    if ($null -eq $python) {
        throw "Wyzer could not prepare its private 64-bit Python 3.11 runtime."
    }
    return $python
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = @($machinePath, $userPath, $env:Path) -join ";"
}

function Find-Ollama {
    Refresh-ProcessPath
    $command = Get-Command ollama -ErrorAction SilentlyContinue
    if ($null -ne $command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        return $command.Source
    }
    $knownLocations = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        (Join-Path $env:ProgramFiles "Ollama\ollama.exe")
    )
    foreach ($candidate in $knownLocations) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Install-Ollama {
    $ollama = Find-Ollama
    if ($null -ne $ollama) { return $ollama }

    Write-InstallStep "Installing Ollama for Wyzer's local AI"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($null -ne $winget) {
        & $winget.Source install --id Ollama.Ollama --exact --source winget --silent `
            --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "WinGet could not install Ollama. Trying Ollama's official installer."
        }
        $ollama = Find-Ollama
    }

    if ($null -eq $ollama) {
        $installer = Join-Path ([IO.Path]::GetTempPath()) "wyzer-ollama-setup.exe"
        try {
            Save-TrustedDownload `
                "https://ollama.com/download/OllamaSetup.exe" `
                $installer `
                @("Ollama")
            $process = Start-Process -FilePath $installer `
                -ArgumentList "/VERYSILENT /NORESTART" -Wait -PassThru
            if ($process.ExitCode -ne 0) {
                throw "The Ollama installer returned exit code $($process.ExitCode)."
            }
        } finally {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
        $ollama = Find-Ollama
    }

    if ($null -eq $ollama) {
        throw "Ollama was installed, but Wyzer could not find ollama.exe."
    }
    return $ollama
}

function Test-OllamaReady {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:11434/api/tags" `
            -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Start-OllamaAndWait([string]$Executable) {
    if (Test-OllamaReady) { return }
    Start-Process -FilePath $Executable -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-OllamaReady) { return }
        Start-Sleep -Seconds 1
    }
    throw "Ollama did not become ready at http://127.0.0.1:11434."
}

function Install-OllamaModel(
    [string]$Executable,
    [string]$Model
) {
    Start-OllamaAndWait $Executable
    & $Executable show $Model 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Local AI model is already ready: $Model"
        return
    }
    Write-InstallStep "Downloading the local AI model ($Model)"
    Write-Host "This is the largest download and may take a while. It resumes if interrupted."
    & $Executable pull $Model
    if ($LASTEXITCODE -ne 0) { throw "Ollama could not download $Model." }
    & $Executable show $Model 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Ollama did not report $Model after downloading it." }
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
if ($null -eq $python) {
    $python = Install-PrivatePython311
} else {
    Write-Host "Using compatible Python: $($python.Executable)"
}
$venv = Join-Path $InstallRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-InstallStep "Creating Wyzer's private app environment"
    & $python.Executable @($python.Prefix) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Wyzer virtual environment." }
}

Write-InstallStep "Installing Wyzer and its tested dependencies"
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

$llmDetailsOutput = & $venvPython -c "import json; from wyzer.config import WyzerSettings; from wyzer.runtime_paths import configure_runtime_paths, find_config_path; p=find_config_path(); s=configure_runtime_paths(WyzerSettings.load(p), p); print(json.dumps({'provider': s.llm.provider, 'model': s.llm.model, 'endpoint': str(s.llm.endpoint) if s.llm.endpoint else ''}))" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $llmDetailsOutput) {
    throw "Could not read the local AI settings from $configTarget"
}
$llmDetails = [string]($llmDetailsOutput | Select-Object -Last 1) | ConvertFrom-Json
$localOllama = $llmDetails.provider -eq "ollama" -and `
    ([Uri]$llmDetails.endpoint).Host -in @("127.0.0.1", "localhost")
if ($localOllama -and -not $SkipLocalAISetup) {
    $ollama = Install-Ollama
    if (-not $SkipLlmModelDownload) {
        Install-OllamaModel $ollama ([string]$llmDetails.model)
    } else {
        Write-Warning "The local AI model download was skipped. Chat will not work until '$($llmDetails.model)' is pulled with Ollama."
    }
} elseif ($localOllama) {
    Write-Warning "Local AI setup was skipped. Install Ollama and pull '$($llmDetails.model)' before using chat."
}

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

$shortcutPath = $null
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

    $startMenuDirectory = Join-Path ([Environment]::GetFolderPath("Programs")) "Wyzer"
    New-Item -ItemType Directory -Force -Path $startMenuDirectory | Out-Null
    $startMenuShortcutPath = Join-Path $startMenuDirectory "Wyzer.lnk"
    Copy-Item -LiteralPath $shortcutPath -Destination $startMenuShortcutPath -Force
}

Write-InstallStep "Running the final readiness check"
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
Write-Host "Wyzer is ready." -ForegroundColor Green
Write-Host "Installed at: $InstallRoot"
Write-Host "Install log: $installLog"
Write-Host "Use 'Start Wyzer.cmd' or the desktop shortcut to launch it."
Write-Host "Use 'Wyzer Console.cmd' if you need to see startup errors."

if (-not $NoLaunch) {
    Write-Host "Starting Wyzer..."
    if ($null -ne $shortcutPath -and (Test-Path -LiteralPath $shortcutPath)) {
        Start-Process -FilePath $shortcutPath | Out-Null
    } else {
        Start-Process -FilePath (Join-Path $env:WINDIR "System32\wscript.exe") `
            -ArgumentList "`"$hiddenLauncherPath`"" | Out-Null
    }
}
