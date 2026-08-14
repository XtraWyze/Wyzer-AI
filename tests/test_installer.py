from pathlib import Path

INSTALLER_DIRECTORY = Path(__file__).parents[1] / "installer"


def test_double_click_launcher_uses_process_scoped_execution_policy_bypass() -> None:
    launcher = (INSTALLER_DIRECTORY / "Install Wyzer.cmd").read_text(encoding="utf-8")

    assert "-ExecutionPolicy Bypass" in launcher
    assert '-File "%WYZER_INSTALLER%" %*' in launcher
    assert "Set-ExecutionPolicy" not in launcher


def test_double_click_launcher_points_failures_to_the_install_log() -> None:
    launcher = (INSTALLER_DIRECTORY / "Install Wyzer.cmd").read_text(encoding="utf-8")

    assert r"%LOCALAPPDATA%\Wyzer\install.log" in launcher


def test_release_builder_includes_double_click_launcher() -> None:
    builder = (INSTALLER_DIRECTORY / "build-release.ps1").read_text(encoding="utf-8")

    assert 'Join-Path $PSScriptRoot "Install Wyzer.cmd"' in builder


def test_release_bundles_verified_openwakeword_support_models() -> None:
    builder = (INSTALLER_DIRECTORY / "build-release.ps1").read_text(encoding="utf-8")
    installer = (INSTALLER_DIRECTORY / "install.ps1").read_text(encoding="utf-8")

    for name, sha256 in {
        "melspectrogram.onnx": "BA2B0E0F8B7B875369A2C89CB13360FF53BAC436F2895CCED9F479FA65EB176F",
        "embedding_model.onnx": "70D164290C1D095D1D4EE149BC5E00543250A7316B59F31D056CFF7BD3075C1F",
    }.items():
        assert name in builder
        assert name in installer
        assert sha256 in builder
        assert sha256 in installer
    assert "assets\\openwakeword-support" in builder
    assert "assets\\openwakeword-support" in installer


def test_installer_copies_and_verifies_wake_models_to_configured_directory() -> None:
    builder = (INSTALLER_DIRECTORY / "build-release.ps1").read_text(encoding="utf-8")
    installer = (INSTALLER_DIRECTORY / "install.ps1").read_text(encoding="utf-8")

    for name, sha256 in {
        "hey_Wyzer.onnx": "DFCADF0902C52F230E59D671D4AD6FC86A3E7116FCF751BB4818334F54539700",
        "hey_wiser.onnx": "1B44C4161528E158CEF225DA38167120317CA093F5A1F5BA4C0BFB391EA08591",
    }.items():
        assert name in builder
        assert name in installer
        assert sha256 in builder
        assert sha256 in installer
    assert "configuredWakeDirectory" in installer
    assert "Install-WakeWordModels" in installer
    assert 'Get-ChildItem -LiteralPath $destination -Filter "*.onnx"' in installer
    assert '"--output", $readinessReport' in installer


def test_fresh_windows_install_bootstraps_signed_python_and_ollama() -> None:
    installer = (INSTALLER_DIRECTORY / "install.ps1").read_text(encoding="utf-8")

    assert "Install-PrivatePython311" in installer
    assert "Python Software Foundation" in installer
    assert "Get-AuthenticodeSignature" in installer
    assert "Python311\\python.exe" in installer
    assert "Ollama.Ollama" in installer
    assert "https://ollama.com/download/OllamaSetup.exe" in installer
    assert "--accept-package-agreements" in installer
    assert "--disable-interactivity" in installer


def test_fresh_windows_install_bootstraps_the_native_runtime() -> None:
    installer = (INSTALLER_DIRECTORY / "install.ps1").read_text(encoding="utf-8")

    assert "Test-VisualCppRuntime" in installer
    assert "Install-VisualCppRuntime" in installer
    assert "https://aka.ms/vc14/vc_redist.x64.exe" in installer
    assert "Microsoft Corporation" in installer
    assert '"/install /quiet /norestart"' in installer
    assert installer.index("Install-VisualCppRuntime\n") < installer.index(
        '$venv = Join-Path $InstallRoot ".venv"'
    )


def test_openwakeword_model_locator_does_not_import_the_package() -> None:
    installer = (INSTALLER_DIRECTORY / "install.ps1").read_text(encoding="utf-8")

    assert "find_spec('openwakeword')" in installer
    assert "import openwakeword; print(Path(openwakeword.__file__)" not in installer


def test_installer_pulls_the_model_from_the_effective_config_and_launches() -> None:
    installer = (INSTALLER_DIRECTORY / "install.ps1").read_text(encoding="utf-8")

    assert "Install-OllamaModel $ollama ([string]$llmDetails.model)" in installer
    assert "Test-OllamaModel $Model" in installer
    assert "Invoke-RestMethod -UseBasicParsing `" in installer
    assert '-ArgumentList @("pull", $Model)' in installer
    assert "& $Executable show $Model" not in installer
    assert 'Join-Path ([Environment]::GetFolderPath("Programs")) "Wyzer"' in installer
    assert "if (-not $NoLaunch)" in installer
    assert 'Join-Path $InstallRoot "install.log"' in installer


def test_release_guide_has_no_manual_python_or_ollama_prerequisite() -> None:
    guide = (INSTALLER_DIRECTORY / "RELEASE_README.txt").read_text(encoding="utf-8")

    assert "You do not need to open PowerShell" in guide
    assert "winget install -e --id Python.Python.3.11" not in guide
    assert "ollama pull qwen3.5:4b" not in guide
