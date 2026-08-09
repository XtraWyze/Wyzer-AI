"""Provider-independent native chat and tool-calling boundary."""

from __future__ import annotations

from collections import deque
from typing import Protocol

from wyzer.models import (
    ChatMessage,
    ChatRequestSettings,
    NativeToolDefinition,
    ProviderChatResponse,
)


class ChatProvider(Protocol):
    @property
    def available(self) -> bool: ...

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[NativeToolDefinition],
        settings: ChatRequestSettings | None = None,
    ) -> ProviderChatResponse: ...


class FakeChatProvider:
    """Scripted provider used by deterministic tests."""

    def __init__(
        self,
        responses: list[ProviderChatResponse] | None = None,
        *,
        available: bool = True,
    ) -> None:
        self._responses = deque(responses or [])
        self._available = available
        self.requests: list[tuple[list[ChatMessage], list[NativeToolDefinition]]] = []
        self.request_settings: list[ChatRequestSettings | None] = []

    @property
    def available(self) -> bool:
        return self._available

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[NativeToolDefinition],
        settings: ChatRequestSettings | None = None,
    ) -> ProviderChatResponse:
        self.requests.append((list(messages), list(tools)))
        self.request_settings.append(settings)
        if not self.available:
            raise RuntimeError("local LLM is unavailable")
        if not self._responses:
            raise RuntimeError("fake provider has no scripted response")
        return self._responses.popleft()
