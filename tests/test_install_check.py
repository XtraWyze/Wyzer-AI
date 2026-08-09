from typing import Any

from wyzer import install_check


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
