from pathlib import Path

import pytest
from pydantic import ValidationError

from wyzer.config import WyzerSettings


def test_defaults_work_without_llm() -> None:
    settings = WyzerSettings.load(environ={})
    assert settings.llm.provider == "none"
    assert settings.maximum_tool_rounds == 6
    assert settings.llm.think is False
    assert settings.llm.max_output_tokens == 256
    assert settings.llm.detailed_output_tokens == 1024
    assert settings.audio.default_master_volume_step == 10
    assert settings.tool_packs.enabled == []
    assert settings.task_engine.enabled is True
    assert settings.task_engine.maximum_steps == 12


def test_environment_overrides_typed_values() -> None:
    settings = WyzerSettings.load(
        environ={
            "WYZER_MAX_TOOL_ROUNDS": "7",
            "WYZER_CONFIRMATION_TTL_SECONDS": "90",
            "WYZER_AUDIO_MASTER_STEP": "15",
            "WYZER_MEMORY_ENABLED": "false",
            "WYZER_SPEECH_ENABLED": "true",
            "WYZER_WAKE_PHRASE": "computer listen",
            "WYZER_TOOL_PACKS": "clipboard, home_assistant",
            "WYZER_LLM_MAX_OUTPUT_TOKENS": "128",
            "WYZER_LLM_DETAILED_OUTPUT_TOKENS": "512",
        }
    )
    assert settings.maximum_tool_rounds == 7
    assert settings.confirmation_ttl_seconds == 90
    assert settings.audio.default_master_volume_step == 15
    assert settings.memory.enabled is False
    assert settings.speech.enabled is True
    assert settings.speech.wake_phrase == "computer listen"
    assert settings.tool_packs.enabled == ["clipboard", "home_assistant"]
    assert settings.llm.max_output_tokens == 128
    assert settings.llm.detailed_output_tokens == 512


def test_toml_and_environment_precedence(tmp_path: Path) -> None:
    path = tmp_path / "wyzer.toml"
    path.write_text("maximum_tool_rounds = 5\n[llm]\nprovider = 'ollama'\n", encoding="utf-8")
    settings = WyzerSettings.load(path, {"WYZER_MAX_TOOL_ROUNDS": "8"})
    assert settings.maximum_tool_rounds == 8
    assert settings.llm.provider == "ollama"


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        WyzerSettings.load(environ={"WYZER_LLM_PROVIDER": "imaginary"})


def test_api_key_is_secret_and_loaded_from_environment() -> None:
    settings = WyzerSettings.load(environ={"WYZER_LLM_API_KEY": "local-secret"})
    assert settings.llm.api_key is not None
    assert settings.llm.api_key.get_secret_value() == "local-secret"
    assert "local-secret" not in repr(settings)


def test_obsolete_planner_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WyzerSettings.model_validate({"maximum_plan_steps": 12})
    with pytest.raises(ValidationError):
        WyzerSettings.model_validate({"llm": {"planning_temperature": 0.1}})
