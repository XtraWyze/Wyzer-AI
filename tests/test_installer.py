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
