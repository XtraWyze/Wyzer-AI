[CmdletBinding()]
param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$openWakeWordSupportModels = @(
    [PSCustomObject]@{
        Name = "melspectrogram.onnx"
        Url = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx"
        Sha256 = "BA2B0E0F8B7B875369A2C89CB13360FF53BAC436F2895CCED9F479FA65EB176F"
    },
    [PSCustomObject]@{
        Name = "embedding_model.onnx"
        Url = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx"
        Sha256 = "70D164290C1D095D1D4EE149BC5E00543250A7316B59F31D056CFF7BD3075C1F"
    }
)

function Copy-OpenWakeWordSupportModels([string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $installedRoot = & py -3.11 -c "from pathlib import Path; import openwakeword; print(Path(openwakeword.__file__).resolve().parent / 'resources' / 'models')" 2>$null
    if ($LASTEXITCODE -ne 0) { $installedRoot = "" }
    $installedRoot = [string]($installedRoot | Select-Object -Last 1)

    foreach ($model in $openWakeWordSupportModels) {
        $target = Join-Path $Destination $model.Name
        $installed = if ($installedRoot) { Join-Path $installedRoot $model.Name } else { "" }
        if ($installed -and (Test-Path -LiteralPath $installed)) {
            $installedHash = (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash
            if ($installedHash -eq $model.Sha256) {
                Copy-Item -LiteralPath $installed -Destination $target -Force
            }
        }
        if (-not (Test-Path -LiteralPath $target)) {
            Write-Host "Downloading pinned OpenWakeWord support model $($model.Name)..."
            Invoke-WebRequest -UseBasicParsing -Uri $model.Url -OutFile $target
        }
        $actualHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($actualHash -ne $model.Sha256) {
            Remove-Item -LiteralPath $target -Force
            throw "OpenWakeWord support model $($model.Name) failed its SHA-256 check."
        }
    }
}

$sourceRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $sourceRoot "dist\Wyzer-Setup"
}
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
$resolvedSource = [System.IO.Path]::GetFullPath($sourceRoot)
$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedSource "dist"))
$releasePrefix = $releaseRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedOutput.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The release output must be a child directory of $releaseRoot."
}

if (Test-Path -LiteralPath $resolvedOutput) {
    Remove-Item -LiteralPath $resolvedOutput -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $resolvedOutput "assets\avatar") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $resolvedOutput "assets\wake-models") | Out-Null
Copy-OpenWakeWordSupportModels (Join-Path $resolvedOutput "assets\openwakeword-support")

py -3.11 -m pip wheel $sourceRoot --no-deps --no-build-isolation --wheel-dir $resolvedOutput
if ($LASTEXITCODE -ne 0) { throw "Could not build the Wyzer wheel." }

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.ps1") -Destination $resolvedOutput -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Install Wyzer.cmd") -Destination $resolvedOutput -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "constraints-windows-py311.txt") -Destination $resolvedOutput -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "RELEASE_README.txt") -Destination (Join-Path $resolvedOutput "README.txt") -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "wyzer.toml") -Destination $resolvedOutput -Force
$avatarSource = Join-Path $sourceRoot ".wyzer\avatar"
if (Test-Path -LiteralPath $avatarSource) {
    Copy-Item -Path (Join-Path $avatarSource "*") -Destination (Join-Path $resolvedOutput "assets\avatar") -Force
} else {
    Write-Warning "No custom avatar frames found. The installer will use Wyzer's built-in mascot."
}
Copy-Item -Path (Join-Path $sourceRoot "openwakemodels\*") -Destination (Join-Path $resolvedOutput "assets\wake-models") -Force

$zipPath = Join-Path (Split-Path -Parent $resolvedOutput) "Wyzer-Setup.zip"
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath }
Compress-Archive -Path (Join-Path $resolvedOutput "*") -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Release created: $zipPath"
