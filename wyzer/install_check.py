"""Installation and model readiness checks used by the Windows installer."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import struct
import sys
from collections.abc import Sequence
from pathlib import Path

from wyzer.config import WyzerSettings
from wyzer.runtime_paths import configure_runtime_paths, data_home, find_config_path

REQUIRED_MODULES = (
    "pydantic",
    # Load ONNX Runtime before Qt's native DLLs.  The reverse order can make a
    # healthy OpenWakeWord installation fail its import on Windows.
    "openwakeword",
    "PySide6",
    "sounddevice",
    "faster_whisper",
    "kokoro",
    "torch",
)
OPENWAKEWORD_SUPPORT_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")


def _whisper_present(settings: WyzerSettings) -> bool:
    model_key = settings.speech.whisper_model.replace("/", "--")
    root = settings.speech.whisper_download_root
    direct = root / f"models--Systran--faster-whisper-{model_key}" / "snapshots"
    return direct.is_dir() and any(item.is_dir() for item in direct.iterdir())


def _download_whisper(settings: WyzerSettings) -> None:
    from huggingface_hub import snapshot_download

    settings.speech.whisper_download_root.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=f"Systran/faster-whisper-{settings.speech.whisper_model}",
        cache_dir=settings.speech.whisper_download_root,
    )


def _openwakeword_support_directory() -> Path:
    module = importlib.import_module("openwakeword")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise RuntimeError("Could not locate the installed OpenWakeWord package")
    return Path(module_file).resolve().parent / "resources" / "models"


def _missing_openwakeword_support_models() -> list[str]:
    try:
        root = _openwakeword_support_directory()
    except Exception:
        return list(OPENWAKEWORD_SUPPORT_MODELS)
    return [name for name in OPENWAKEWORD_SUPPORT_MODELS if not (root / name).is_file()]


def _download_openwakeword_support_models() -> None:
    import openwakeword
    import requests

    root = _openwakeword_support_directory()
    root.mkdir(parents=True, exist_ok=True)
    feature_models = getattr(openwakeword, "FEATURE_MODELS", {})
    urls = {
        Path(str(details["download_url"])).name.replace(".tflite", ".onnx"): str(
            details["download_url"]
        ).replace(".tflite", ".onnx")
        for details in feature_models.values()
    }
    for name in _missing_openwakeword_support_models():
        url = urls.get(name)
        if not url:
            raise RuntimeError(f"OpenWakeWord did not publish a download URL for {name}")
        target = root / name
        temporary = target.with_suffix(target.suffix + ".download")
        try:
            with requests.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def _torch_details() -> dict[str, object]:
    details: dict[str, object] = {
        "version": None,
        "cuda_build": None,
        "cuda_available": False,
        "device_count": 0,
    }
    try:
        torch = importlib.import_module("torch")
        details = {
            "version": str(getattr(torch, "__version__", "unknown")),
            "cuda_build": getattr(getattr(torch, "version", None), "cuda", None),
            "cuda_available": bool(torch.cuda.is_available()),
            "device_count": int(torch.cuda.device_count()),
        }
    except Exception as error:
        details["error"] = str(error)
    return details


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check whether Wyzer is ready to run")
    parser.add_argument("--download-model", action="store_true")
    parser.add_argument("--allow-missing-model", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    path = find_config_path()
    settings = configure_runtime_paths(WyzerSettings.load(path), path)
    imports: dict[str, str] = {}
    failures: list[str] = []
    for module_name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(module_name)
            imports[module_name] = str(getattr(module, "__version__", "installed"))
        except Exception as error:
            imports[module_name] = f"ERROR: {error}"
            failures.append(f"Could not import {module_name}")

    if args.download_model and not _whisper_present(settings):
        try:
            _download_whisper(settings)
        except Exception as error:
            failures.append(f"Could not download the Whisper model: {error}")

    if args.download_model and _missing_openwakeword_support_models():
        try:
            _download_openwakeword_support_models()
        except Exception as error:
            failures.append(f"Could not download OpenWakeWord support models: {error}")

    whisper_ready = _whisper_present(settings)
    missing_wake_support = _missing_openwakeword_support_models()
    wake_models = list(settings.speech.wake_model_directory.glob("*.onnx"))
    avatar_frames = list((data_home() / "avatar").glob("*.png")) + list(
        (data_home() / "avatar").glob("*.webp")
    )
    torch_details = _torch_details()
    if settings.speech.tts_device == "cuda" and not torch_details["cuda_available"]:
        failures.append("Text-to-speech is configured for CUDA, but PyTorch cannot use CUDA")
    if not wake_models:
        failures.append(
            "No wake-word ONNX model was installed in "
            + str(settings.speech.wake_model_directory)
        )
    if missing_wake_support and not args.allow_missing_model:
        failures.append(
            "OpenWakeWord support models are missing: " + ", ".join(missing_wake_support)
        )
    # Custom avatar frames are optional.  The desktop UI deliberately falls
    # back to its built-in vector mascot when this directory is empty.
    if not whisper_ready and not args.allow_missing_model:
        failures.append("The configured Whisper model is not installed")
    if sys.version_info[:2] != (3, 11) or struct.calcsize("P") * 8 != 64:
        failures.append("Wyzer requires 64-bit Python 3.11")

    result = {
        "ok": not failures,
        "python": sys.version.split()[0],
        "bits": struct.calcsize("P") * 8,
        "data_home": str(data_home()),
        "config": str(path) if path else None,
        "imports": imports,
        "avatar_frames": len(avatar_frames),
        "wake_models": len(wake_models),
        "wake_model_directory": str(settings.speech.wake_model_directory),
        "wake_model_files": sorted(path.name for path in wake_models),
        "openwakeword_support_models_ready": not missing_wake_support,
        "whisper_model_ready": whisper_ready,
        "torch": torch_details,
        "failures": failures,
    }
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
