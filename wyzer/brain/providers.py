"""Native chat/tool providers for Ollama and compatible local servers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from wyzer.brain.http import HttpTransportError, JsonTransport, UrllibJsonTransport
from wyzer.models import (
    ChatMessage,
    ChatRequestSettings,
    NativeFunctionCall,
    NativeToolCall,
    NativeToolDefinition,
    ProviderChatResponse,
    ProviderDiagnostic,
)


class LLMProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NativeChatProvider:
    provider_name: str

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        temperature: float = 0.1,
        think: bool = False,
        max_output_tokens: int = 256,
        timeout_seconds: float = 60,
        transport: JsonTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.think = think
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()

    @property
    def available(self) -> bool:
        return bool(self.endpoint and self.model)

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[NativeToolDefinition],
        settings: ChatRequestSettings | None = None,
    ) -> ProviderChatResponse:
        if not self.available:
            raise LLMProviderError("PROVIDER_NOT_CONFIGURED", "Local model is not configured.")
        try:
            return await self._chat(messages, tools, settings)
        except HttpTransportError as error:
            raise LLMProviderError("PROVIDER_REQUEST_FAILED", str(error)) from error

    async def diagnose(self) -> ProviderDiagnostic:
        if not self.available:
            return ProviderDiagnostic(
                provider=self.provider_name,
                available=False,
                endpoint=self.endpoint or None,
                model=self.model or None,
                message="Provider requires both an endpoint and model name.",
            )
        try:
            details = await self._diagnostic_request()
        except (HttpTransportError, LLMProviderError) as error:
            return ProviderDiagnostic(
                provider=self.provider_name,
                available=False,
                endpoint=self.endpoint,
                model=self.model,
                message=str(error),
            )
        found = details.get("configured_model_found")
        return ProviderDiagnostic(
            provider=self.provider_name,
            available=found is not False,
            endpoint=self.endpoint,
            model=self.model,
            message=(
                "Local model endpoint is reachable."
                if found is not False
                else "Endpoint is reachable, but the configured model was not advertised."
            ),
            details=details,
        )

    async def warm_up(self) -> None:
        """Load the configured model before the first user request."""
        if not self.available:
            raise LLMProviderError("PROVIDER_NOT_CONFIGURED", "Local model is not configured.")
        try:
            await self._warm_up_request()
        except HttpTransportError as error:
            raise LLMProviderError("PROVIDER_WARMUP_FAILED", str(error)) from error

    async def _chat(
        self,
        messages: list[ChatMessage],
        tools: list[NativeToolDefinition],
        settings: ChatRequestSettings | None,
    ) -> ProviderChatResponse:
        raise NotImplementedError

    async def _diagnostic_request(self) -> dict[str, Any]:
        raise NotImplementedError

    async def _warm_up_request(self) -> None:
        raise NotImplementedError


class OllamaChatProvider(NativeChatProvider):
    provider_name = "ollama"

    def __init__(
        self,
        *args: Any,
        auto_start: bool = False,
        startup_timeout_seconds: float = 10,
        keep_alive: str = "30m",
        context_length: int = 32_768,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.auto_start = auto_start
        self.startup_timeout_seconds = startup_timeout_seconds
        self.keep_alive = keep_alive
        self.context_length = context_length
        self._startup_lock = asyncio.Lock()

    async def _chat(
        self,
        messages: list[ChatMessage],
        tools: list[NativeToolDefinition],
        settings: ChatRequestSettings | None,
    ) -> ProviderChatResponse:
        active = settings or ChatRequestSettings(
            temperature=self.temperature,
            think=self.think,
            max_output_tokens=self.max_output_tokens,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_ollama_message(message) for message in messages],
            "stream": False,
            "think": active.think,
            "options": {
                "temperature": active.temperature,
                "num_predict": active.max_output_tokens,
                "num_ctx": self.context_length,
            },
            "keep_alive": self.keep_alive,
        }
        if tools:
            payload["tools"] = [tool.model_dump(mode="json") for tool in tools]
        try:
            response = await self._request_with_recovery(
                "POST",
                f"{self.endpoint}/api/chat",
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except HttpTransportError as error:
            if not tools or not _is_qwen_tool_parse_failure(error):
                raise
            # Ollama 0.24.x can return HTTP 500 before the client receives a
            # malformed Qwen 3.5 tool call. No tool has executed at this point,
            # so one conservative native-tool retry is safe.
            retry_payload = _qwen_tool_retry_payload(payload)
            response = await self._request_with_recovery(
                "POST",
                f"{self.endpoint}/api/chat",
                payload=retry_payload,
                timeout_seconds=self.timeout_seconds,
            )
        raw = response.data.get("message")
        if not isinstance(raw, dict):
            raise LLMProviderError(
                "INVALID_PROVIDER_RESPONSE", "Ollama returned no assistant message."
            )
        return ProviderChatResponse(
            message=_parse_assistant_message(raw),
            metadata={
                key: response.data[key]
                for key in ("model", "done_reason", "total_duration", "eval_count")
                if key in response.data
            },
        )

    async def _diagnostic_request(self) -> dict[str, Any]:
        response = await self._request_with_recovery(
            "GET", f"{self.endpoint}/api/tags", timeout_seconds=min(self.timeout_seconds, 5)
        )
        models = response.data.get("models", [])
        names = [item.get("name") for item in models if isinstance(item, dict)]
        return {"configured_model_found": self.model in names, "model_count": len(names)}

    async def _warm_up_request(self) -> None:
        # An empty generation loads the model without producing visible text.
        await self._request_with_recovery(
            "POST",
            f"{self.endpoint}/api/generate",
            payload={
                "model": self.model,
                "prompt": "",
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": {"num_ctx": self.context_length},
            },
            timeout_seconds=self.timeout_seconds,
        )

    async def _request_with_recovery(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return await self._transport.request(method, url, **kwargs)
        except HttpTransportError as error:
            if not self.auto_start or not self._is_local_endpoint() or error.status is not None:
                raise
            connection_error = error
        async with self._startup_lock:
            if not self._start_local_service():
                raise connection_error
            deadline = time.monotonic() + self.startup_timeout_seconds
            last_error: HttpTransportError | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                try:
                    return await self._transport.request(method, url, **kwargs)
                except HttpTransportError as error:
                    last_error = error
            if last_error is not None:
                raise last_error
        raise HttpTransportError("Ollama did not become ready before the startup timeout.")

    def _is_local_endpoint(self) -> bool:
        return (urlparse(self.endpoint).hostname or "").casefold() in {"127.0.0.1", "localhost"}

    def _start_local_service(self) -> bool:
        executable = shutil.which("ollama")
        if executable is None:
            return False
        environment = os.environ.copy()
        environment["OLLAMA_CONTEXT_LENGTH"] = str(self.context_length)
        try:
            subprocess.Popen(
                [executable, "serve"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return False
        return True


class OpenAICompatibleChatProvider(NativeChatProvider):
    provider_name = "openai_compatible"

    def __init__(self, *args: Any, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _chat(
        self,
        messages: list[ChatMessage],
        tools: list[NativeToolDefinition],
        settings: ChatRequestSettings | None,
    ) -> ProviderChatResponse:
        active = settings or ChatRequestSettings(
            temperature=self.temperature,
            think=self.think,
            max_output_tokens=self.max_output_tokens,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_openai_message(message) for message in messages],
            "temperature": active.temperature,
            "max_tokens": active.max_output_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = [tool.model_dump(mode="json") for tool in tools]
        response = await self._transport.request(
            "POST",
            f"{self.endpoint}/chat/completions",
            payload=payload,
            headers=self._headers(),
            timeout_seconds=self.timeout_seconds,
        )
        choices = response.data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        raw = first.get("message") if isinstance(first, dict) else None
        if not isinstance(raw, dict):
            raise LLMProviderError(
                "INVALID_PROVIDER_RESPONSE", "Compatible endpoint returned no assistant message."
            )
        return ProviderChatResponse(message=_parse_assistant_message(raw))

    async def _diagnostic_request(self) -> dict[str, Any]:
        response = await self._transport.request(
            "GET",
            f"{self.endpoint}/models",
            headers=self._headers(),
            timeout_seconds=min(self.timeout_seconds, 5),
        )
        data = response.data.get("data", [])
        names = [item.get("id") for item in data if isinstance(item, dict)]
        return {"configured_model_found": self.model in names, "model_count": len(names)}

    async def _warm_up_request(self) -> None:
        # Compatible servers have no standard load-only endpoint. A one-token
        # completion forces the configured local model to become resident.
        await self._transport.request(
            "POST",
            f"{self.endpoint}/chat/completions",
            payload={
                "model": self.model,
                "messages": [{"role": "user", "content": "Ready"}],
                "temperature": 0.0,
                "max_tokens": 1,
                "stream": False,
            },
            headers=self._headers(),
            timeout_seconds=self.timeout_seconds,
        )


class LlamaCppChatProvider(OpenAICompatibleChatProvider):
    provider_name = "llama_cpp"


def _is_qwen_tool_parse_failure(error: HttpTransportError) -> bool:
    if error.status != 500:
        return False
    message = str(error).casefold()
    indicators = (
        "xml syntax error",
        "tool call parsing failed",
        "expected element type <function>",
        "element <function>",
        "failed to parse json: unexpected end of json input",
    )
    return any(indicator in message for indicator in indicators)


def _qwen_tool_retry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    retry = dict(payload)
    retry["think"] = False
    options = dict(payload.get("options") or {})
    options["temperature"] = 0.0
    retry["options"] = options

    reminder = (
        "Tool-call recovery: when a tool is needed, emit exactly one valid native function call "
        "using the provided tool schema. Do not write tool-call XML or prose around the call."
    )
    messages = [dict(item) for item in payload.get("messages", []) if isinstance(item, dict)]
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = str(messages[0].get("content") or "") + "\n\n" + reminder
    else:
        messages.insert(0, {"role": "system", "content": reminder})
    retry["messages"] = messages
    return retry


def _parse_assistant_message(raw: dict[str, Any]) -> ChatMessage:
    content = raw.get("content")
    if content is not None and not isinstance(content, str):
        content = str(content)
    calls: list[NativeToolCall] = []
    raw_calls = raw.get("tool_calls", [])
    if not isinstance(raw_calls, list):
        raise LLMProviderError("INVALID_PROVIDER_RESPONSE", "Tool calls were not a list.")
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
                arguments = (
                    decoded if isinstance(decoded, dict) else {"__invalid_json__": arguments}
                )
            except json.JSONDecodeError:
                arguments = {"__invalid_json__": arguments}
        if not isinstance(arguments, dict):
            arguments = {"__invalid_arguments__": arguments}
        calls.append(
            NativeToolCall(
                id=str(raw_call["id"]) if raw_call.get("id") is not None else None,
                function=NativeFunctionCall(name=function["name"], arguments=arguments),
            )
        )
    return ChatMessage(role="assistant", content=content, tool_calls=calls)


def _ollama_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.name is not None:
        payload["tool_name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _openai_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.name is not None:
        payload["name"] = message.name
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id or f"call_{index}",
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": json.dumps(call.function.arguments, separators=(",", ":")),
                },
            }
            for index, call in enumerate(message.tool_calls)
        ]
    return payload
