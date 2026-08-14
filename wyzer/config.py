"""Typed configuration loaded from TOML and WYZER_ environment variables."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

from wyzer.coding.models import CodingAgentSettings


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMSettings(StrictSettings):
    provider: str = "none"
    model: str = ""
    endpoint: HttpUrl | None = None
    temperature: float = Field(default=0.1, ge=0, le=2)
    think: bool = False
    max_output_tokens: int = Field(default=256, ge=32, le=4_096)
    detailed_output_tokens: int = Field(default=1_024, ge=64, le=4_096)
    context_length: int = Field(default=32_768, ge=2_048, le=262_144)
    request_timeout_seconds: float = Field(default=60, gt=0, le=600)
    api_key: SecretStr | None = None
    auto_start: bool = True
    startup_timeout_seconds: float = Field(default=10, gt=0, le=60)
    keep_alive: str = Field(default="30m", min_length=2, max_length=20)


class AudioSettings(StrictSettings):
    default_master_volume_step: int = Field(default=10, ge=1, le=100)
    default_application_volume_step: int = Field(default=10, ge=1, le=100)
    application_audio_match_threshold: float = Field(default=0.72, ge=0, le=1)
    application_audio_ambiguity_margin: float = Field(default=0.08, ge=0, le=1)
    control_all_matching_sessions: bool = True
    core_audio_timeout_seconds: float = Field(default=5, gt=0, le=60)


class PerceptionSettings(StrictSettings):
    enabled: bool = True
    max_image_dimension: int = Field(default=1600, ge=640, le=4096)
    vision_timeout_seconds: float = Field(default=45, gt=0, le=180)
    visual_click_min_confidence: float = Field(default=0.70, ge=0.5, le=1)


class ToolPackSettings(StrictSettings):
    enabled: list[str] = Field(default_factory=list)


class LoggingSettings(StrictSettings):
    level: str = "INFO"
    json_enabled: bool = True
    redact_private_screen_text: bool = True


class MemorySettings(StrictSettings):
    enabled: bool = True
    require_explicit_consent: bool = True
    database_path: Path = Path(".wyzer/memory.db")


class TaskEngineSettings(StrictSettings):
    enabled: bool = True
    state_path: Path = Path(".wyzer/task-state.json")
    maximum_steps: int = Field(default=12, ge=2, le=25)
    maximum_retries_per_step: int = Field(default=2, ge=0, le=10)


class PersonalitySettings(StrictSettings):
    assistant_name: str = "Wyzer"
    tone: str = "neutral"
    response_length: str = "concise"
    humor_level: int = Field(default=0, ge=0, le=5)
    proactivity_level: int = Field(default=1, ge=0, le=5)
    narrate_before_actions: bool = False
    summarize_completed_actions: bool = True


class SpeechSettings(StrictSettings):
    enabled: bool = False
    stt_adapter: str = "faster_whisper"
    tts_adapter: str = "windows_system"
    tts_speed: float = Field(default=1.08, ge=0.5, le=2.0)
    tts_device: str = Field(default="cpu", pattern=r"^(auto|cuda|cpu)$")
    wake_word_adapter: str = "openwakeword"
    wake_model_directory: Path = Path("openwakemodels")
    wake_model: str | None = Field(default=None, min_length=1, max_length=260)
    wake_phrase: str = Field(default="hey wyzer", min_length=2, max_length=80)
    listen_timeout_seconds: float = Field(default=8, gt=0, le=60)
    wake_timeout_seconds: float = Field(default=30, gt=0, le=120)
    minimum_stt_confidence: float = Field(default=0.35, ge=0, le=1)
    whisper_model: str = Field(default="small.en", min_length=1, max_length=100)
    whisper_device: str = Field(default="auto", pattern=r"^(auto|cuda|cpu)$")
    whisper_compute_type: str = Field(default="int8_float16", min_length=1, max_length=40)
    whisper_download_root: Path = Path(".wyzer/models")
    minimum_wake_confidence: float = Field(default=0.55, ge=0, le=1)
    voice: str | None = Field(default=None, min_length=1, max_length=200)
    rate: int = Field(default=0, ge=-10, le=10)
    volume: int = Field(default=100, ge=0, le=100)


class WyzerSettings(StrictSettings):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    maximum_tool_rounds: int = Field(default=6, ge=1, le=50)
    confirmation_ttl_seconds: float = Field(default=120, gt=0, le=3600)
    tool_result_context_characters: int = Field(default=4_000, ge=256, le=100_000)
    tool_worker_count: int = Field(default=2, ge=1, le=32)
    tool_timeout_seconds: float = Field(default=15, gt=0, le=3600)
    worker_isolation_enabled: bool = True
    tool_packs: ToolPackSettings = Field(default_factory=ToolPackSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    event_ledger_size: int = Field(default=500, ge=10, le=100_000)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    task_engine: TaskEngineSettings = Field(default_factory=TaskEngineSettings)
    coding_agent: CodingAgentSettings = Field(default_factory=CodingAgentSettings)
    personality: PersonalitySettings = Field(default_factory=PersonalitySettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)

    @model_validator(mode="after")
    def validate_llm(self) -> WyzerSettings:
        providers = {"none", "ollama", "openai_compatible", "llama_cpp"}
        if self.llm.provider not in providers:
            raise ValueError(f"unsupported LLM provider: {self.llm.provider}")
        return self

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> WyzerSettings:
        data: dict[str, Any] = {}
        if path is not None and path.exists():
            with path.open("rb") as config_file:
                data = tomllib.load(config_file)
        env = os.environ if environ is None else environ
        overrides: dict[str, tuple[tuple[str, ...], Any]] = {
            "WYZER_LLM_PROVIDER": (("llm", "provider"), str),
            "WYZER_LLM_MODEL": (("llm", "model"), str),
            "WYZER_LLM_ENDPOINT": (("llm", "endpoint"), str),
            "WYZER_LLM_API_KEY": (("llm", "api_key"), str),
            "WYZER_LLM_TEMPERATURE": (("llm", "temperature"), float),
            "WYZER_LLM_TIMEOUT_SECONDS": (("llm", "request_timeout_seconds"), float),
            "WYZER_LLM_KEEP_ALIVE": (("llm", "keep_alive"), str),
            "WYZER_LLM_THINK": (("llm", "think"), _parse_bool),
            "WYZER_LLM_MAX_OUTPUT_TOKENS": (("llm", "max_output_tokens"), int),
            "WYZER_LLM_DETAILED_OUTPUT_TOKENS": (("llm", "detailed_output_tokens"), int),
            "WYZER_LLM_CONTEXT_LENGTH": (("llm", "context_length"), int),
            "WYZER_MAX_TOOL_ROUNDS": (("maximum_tool_rounds",), int),
            "WYZER_CONFIRMATION_TTL_SECONDS": (("confirmation_ttl_seconds",), float),
            "WYZER_TOOL_RESULT_CONTEXT_CHARACTERS": (
                ("tool_result_context_characters",),
                int,
            ),
            "WYZER_TOOL_WORKER_COUNT": (("tool_worker_count",), int),
            "WYZER_TOOL_TIMEOUT_SECONDS": (("tool_timeout_seconds",), float),
            "WYZER_TOOL_PACKS": (("tool_packs", "enabled"), _parse_csv),
            "WYZER_PERCEPTION_ENABLED": (("perception", "enabled"), _parse_bool),
            "WYZER_PERCEPTION_MAX_IMAGE_DIMENSION": (("perception", "max_image_dimension"), int),
            "WYZER_PERCEPTION_VISION_TIMEOUT_SECONDS": (
                ("perception", "vision_timeout_seconds"),
                float,
            ),
            "WYZER_PERCEPTION_VISUAL_CLICK_MIN_CONFIDENCE": (
                ("perception", "visual_click_min_confidence"),
                float,
            ),
            "WYZER_AUDIO_MASTER_STEP": (("audio", "default_master_volume_step"), int),
            "WYZER_AUDIO_APPLICATION_STEP": (("audio", "default_application_volume_step"), int),
            "WYZER_WORKER_ISOLATION_ENABLED": (("worker_isolation_enabled",), _parse_bool),
            "WYZER_EVENT_LEDGER_SIZE": (("event_ledger_size",), int),
            "WYZER_MEMORY_ENABLED": (("memory", "enabled"), _parse_bool),
            "WYZER_TASK_ENGINE_ENABLED": (("task_engine", "enabled"), _parse_bool),
            "WYZER_CODING_AGENT_ENABLED": (("coding_agent", "enabled"), _parse_bool),
            "WYZER_ASSISTANT_NAME": (("personality", "assistant_name"), str),
            "WYZER_SPEECH_ENABLED": (("speech", "enabled"), _parse_bool),
            "WYZER_WAKE_PHRASE": (("speech", "wake_phrase"), str),
        }
        for key, (parts, converter) in overrides.items():
            if key in env and env[key] != "":
                _set_nested(data, parts, converter(env[key]))
        return cls.model_validate(data)


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"configuration path {'.'.join(path)} conflicts with a scalar")
        current = child
    current[path[-1]] = value
