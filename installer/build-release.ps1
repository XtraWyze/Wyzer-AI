[CmdletBinding()]
param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
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

py -3.11 -m pip wheel $sourceRoot --no-deps --no-build-isolation --wheel-dir $resolvedOutput
if ($LASTEXITCODE -ne 0) { throw "Could not build the Wyzer wheel." }

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.ps1") -Destination $resolvedOutput -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "constraints-windows-py311.txt") -Destination $resolvedOutput -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "RELEASE_README.txt") -Destination (Join-Path $resolvedOutput "README.txt") -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "wyzer.toml") -Destination $resolvedOutput -Force
Copy-Item -Path (Join-Path $sourceRoot ".wyzer\avatar\*") -Destination (Join-Path $resolvedOutput "assets\avatar") -Force
Copy-Item -Path (Join-Path $sourceRoot "openwakemodels\*") -Destination (Join-Path $resolvedOutput "assets\wake-models") -Force

$zipPath = Join-Path (Split-Path -Parent $resolvedOutput) "Wyzer-Setup.zip"
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath }
Compress-Archive -Path (Join-Path $resolvedOutput "*") -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Release created: $zipPath"
