"""Configuration-driven native chat provider construction."""

from __future__ import annotations

from wyzer.brain.provider import ChatProvider, FakeChatProvider
from wyzer.brain.providers import (
    LlamaCppChatProvider,
    NativeChatProvider,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
)
from wyzer.config import LLMSettings, PersonalitySettings


def create_chat_provider(
    settings: LLMSettings, personality: PersonalitySettings | None = None
) -> ChatProvider:
    del personality
    if settings.provider == "none":
        return FakeChatProvider(available=False)
    endpoint = (
        str(settings.endpoint).rstrip("/")
        if settings.endpoint
        else _default_endpoint(settings.provider)
    )
    common = {
        "temperature": settings.temperature,
        "think": settings.think,
        "max_output_tokens": settings.max_output_tokens,
        "timeout_seconds": settings.request_timeout_seconds,
    }
    if settings.provider == "ollama":
        return OllamaChatProvider(
            endpoint,
            settings.model,
            **common,
            context_length=settings.context_length,
            auto_start=settings.auto_start,
            startup_timeout_seconds=settings.startup_timeout_seconds,
            keep_alive=settings.keep_alive,
        )
    api_key = settings.api_key.get_secret_value() if settings.api_key else None
    if settings.provider == "openai_compatible":
        return OpenAICompatibleChatProvider(endpoint, settings.model, **common, api_key=api_key)
    if settings.provider == "llama_cpp":
        return LlamaCppChatProvider(endpoint, settings.model, **common, api_key=api_key)
    raise ValueError(f"unsupported LLM provider: {settings.provider}")


def diagnostic_provider(provider: ChatProvider) -> NativeChatProvider | None:
    return provider if isinstance(provider, NativeChatProvider) else None


def _default_endpoint(provider: str) -> str:
    if provider == "ollama":
        return "http://127.0.0.1:11434"
    if provider == "llama_cpp":
        return "http://127.0.0.1:8080/v1"
    return "http://127.0.0.1:8000/v1"
