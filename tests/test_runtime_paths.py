from pathlib import Path

from wyzer.config import WyzerSettings
from wyzer.runtime_paths import (
    configure_runtime_paths,
    data_home,
    file_index_path,
    find_config_path,
)


def test_installed_data_home_and_config_are_explicit(tmp_path: Path) -> None:
    home = tmp_path / "Wyzer Data"
    config = home / "wyzer.toml"
    config.parent.mkdir()
    config.write_text("[speech]\nwake_model_directory = 'wake-models'\n", encoding="utf-8")
    env = {"WYZER_HOME": str(home), "WYZER_CONFIG": str(config)}

    assert data_home(env) == home.resolve()
    assert find_config_path(env) == config.resolve()

    settings = configure_runtime_paths(WyzerSettings.load(config, env), config, env)
    assert settings.memory.database_path == home / "memory.db"
    assert settings.task_engine.state_path == home / "task-state.json"
    assert settings.speech.whisper_download_root == home / "models"
    assert settings.speech.wake_model_directory == home / "wake-models"


def test_non_dot_wyzer_relative_paths_follow_config(tmp_path: Path) -> None:
    config = tmp_path / "config" / "wyzer.toml"
    config.parent.mkdir()
    config.write_text(
        "[memory]\ndatabase_path = 'data/memory.db'\n"
        "[task_engine]\nstate_path = 'data/tasks.json'\n",
        encoding="utf-8",
    )
    settings = configure_runtime_paths(WyzerSettings.load(config, {}), config, {})
    assert settings.memory.database_path == config.parent / "data" / "memory.db"
    assert settings.task_engine.state_path == config.parent / "data" / "tasks.json"


def test_file_index_path_is_stable_for_development_and_installed_copies(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert file_index_path({}) == tmp_path / ".wyzer" / "file_index.sqlite3"

    installed = tmp_path / "installed"
    installed.mkdir()
    assert file_index_path({"WYZER_HOME": str(installed)}) == installed / "file_index.sqlite3"


def test_file_index_path_preserves_populated_legacy_install(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    legacy = installed / ".wyzer" / "file_index.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.touch()

    assert file_index_path({"WYZER_HOME": str(installed)}) == legacy
