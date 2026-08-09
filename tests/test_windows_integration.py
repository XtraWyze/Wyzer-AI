import os
import platform
import time

import psutil
import pytest

from wyzer.desktop.windows_backend import CtypesWindowsBackend

pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(platform.system() != "Windows", reason="Windows only"),
    pytest.mark.skipif(
        os.environ.get("WYZER_RUN_WINDOWS_INTEGRATION") != "1",
        reason="set WYZER_RUN_WINDOWS_INTEGRATION=1 to run safe Windows integration tests",
    ),
]


def test_real_windows_read_only_inventory() -> None:
    backend = CtypesWindowsBackend()
    assert backend.list_processes()
    assert backend.list_monitors()
    assert isinstance(backend.list_windows(), list)


def test_real_windows_notepad_launch() -> None:
    backend = CtypesWindowsBackend()
    process_id, _ = backend.launch_application("Notepad")
    assert process_id is not None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not backend.is_process_running(process_id=process_id):
            time.sleep(0.05)
        assert backend.is_process_running(process_id=process_id)
    finally:
        try:
            process = psutil.Process(process_id)
            process.terminate()
            process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
