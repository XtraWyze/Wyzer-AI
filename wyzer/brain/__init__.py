"""Native local-model chat and tool-calling interfaces."""

from wyzer.brain.capability_context import CapabilityContextBuilder, OrchestratorFeatures
from wyzer.brain.factory import create_chat_provider, diagnostic_provider
from wyzer.brain.prompt import SystemPromptBuilder
from wyzer.brain.provider import ChatProvider, FakeChatProvider
from wyzer.brain.providers import (
    LlamaCppChatProvider,
    LLMProviderError,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
)

__all__ = [
    "CapabilityContextBuilder",
    "ChatProvider",
    "FakeChatProvider",
    "LLMProviderError",
    "LlamaCppChatProvider",
    "OllamaChatProvider",
    "OpenAICompatibleChatProvider",
    "OrchestratorFeatures",
    "SystemPromptBuilder",
    "create_chat_provider",
    "diagnostic_provider",
]
