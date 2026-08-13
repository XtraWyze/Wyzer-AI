from pathlib import Path

INSTALLER_DIRECTORY = Path(__file__).parents[1] / "installer"


def test_double_click_launcher_uses_process_scoped_execution_policy_bypass() -> None:
    launcher = (INSTALLER_DIRECTORY / "Install Wyzer.cmd").read_text(encoding="utf-8")

    assert "-ExecutionPolicy Bypass" in launcher
    assert '-File "%WYZER_INSTALLER%" %*' in launcher
    assert "Set-ExecutionPolicy" not in launcher


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
    assert 'assets\\openwakeword-support' in builder
    assert 'assets\\openwakeword-support' in installer


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
