"""Safe read-only Windows Core Audio diagnostic."""

from __future__ import annotations

import json

from wyzer.config import WyzerSettings
from wyzer.desktop.windows_backend import CtypesWindowsBackend
from wyzer.runtime_paths import configure_runtime_paths, find_config_path
from wyzer.tools.base import ToolExecutionError


def main() -> None:
    path = find_config_path()
    settings = configure_runtime_paths(WyzerSettings.load(path), path)
    try:
        backend = CtypesWindowsBackend(audio_options=settings.audio.model_dump())
        print(json.dumps(backend.audio_diagnostic(), ensure_ascii=False, indent=2, default=str))
    except ToolExecutionError as error:
        print(
            json.dumps(
                {"ok": False, "error": {"code": error.code, "message": str(error)}}, indent=2
            )
        )


if __name__ == "__main__":
    main()
