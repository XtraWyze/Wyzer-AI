from pathlib import Path
from typing import Any

import pytest

from wyzer import install_check


def test_install_check_loads_openwakeword_before_qt() -> None:
    assert install_check.REQUIRED_MODULES.index(
        "openwakeword"
    ) < install_check.REQUIRED_MODULES.index("PySide6")


class _FakeCuda:
    @staticmethod
    def is_available() -> bool:
        return True

    @staticmethod
    def device_count() -> int:
        return 1


class _FakeTorch:
    __version__ = "2.3.1+cu121"
    version = type("Version", (), {"cuda": "12.1"})()
    cuda = _FakeCuda()


def test_torch_details_reports_cuda_build(monkeypatch: Any) -> None:
    real_import = install_check.importlib.import_module

    def fake_import(name: str) -> Any:
        return _FakeTorch if name == "torch" else real_import(name)

    monkeypatch.setattr(install_check.importlib, "import_module", fake_import)

    assert install_check._torch_details() == {
        "version": "2.3.1+cu121",
        "cuda_build": "12.1",
        "cuda_available": True,
        "device_count": 1,
    }


def test_missing_custom_avatar_does_not_fail_install_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = install_check.WyzerSettings()
    settings.speech.wake_model_directory = tmp_path
    (tmp_path / "wake.onnx").write_bytes(b"model")

    monkeypatch.setattr(install_check, "REQUIRED_MODULES", ())
    monkeypatch.setattr(install_check, "find_config_path", lambda: None)
    monkeypatch.setattr(install_check.WyzerSettings, "load", lambda path: settings)
    monkeypatch.setattr(install_check, "configure_runtime_paths", lambda value, path: value)
    monkeypatch.setattr(install_check, "data_home", lambda: tmp_path)
    monkeypatch.setattr(install_check, "_whisper_present", lambda value: True)
    monkeypatch.setattr(install_check, "_missing_openwakeword_support_models", lambda: [])
    monkeypatch.setattr(
        install_check,
        "_torch_details",
        lambda: {"version": None, "cuda_available": False},
    )

    install_check.main([])

    assert '"avatar_frames": 0' in capsys.readouterr().out
