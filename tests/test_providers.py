import asyncio
from typing import Any

from wyzer.brain import (
    LlamaCppChatProvider,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
)
from wyzer.brain.http import HttpTransportError, JsonResponse
from wyzer.models import (
    ChatMessage,
    NativeFunctionCall,
    NativeFunctionDefinition,
    NativeToolCall,
    NativeToolDefinition,
)


class FakeTransport:
    def __init__(self, responses: list[JsonResponse] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60,
    ) -> JsonResponse:
        self.calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        if self.error:
            raise self.error
        return self.responses.pop(0)


class FlakyToolParseTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60,
    ) -> JsonResponse:
        del timeout_seconds
        self.calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        if len(self.calls) == 1:
            raise HttpTransportError(
                "Local model endpoint returned HTTP 500: XML syntax error on line 3: "
                "element <function> closed by </parameter>",
                status=500,
            )
        return JsonResponse(
            200,
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "echo", "arguments": {"message": "recovered"}}}
                    ],
                }
            },
        )


def tools() -> list[NativeToolDefinition]:
    return [
        NativeToolDefinition(
            function=NativeFunctionDefinition(
                name="echo",
                description="Echo text.",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            )
        )
    ]


def test_ollama_uses_native_tools_without_execution_plan_format() -> None:
    transport = FakeTransport(
        [
            JsonResponse(
                200,
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "echo",
                                    "arguments": {"message": "hi"},
                                }
                            }
                        ],
                    }
                },
            )
        ]
    )
    provider = OllamaChatProvider("http://127.0.0.1:11434", "test", transport=transport)

    response = asyncio.run(provider.chat([ChatMessage(role="user", content="echo")], tools()))

    assert response.message.tool_calls[0].function.arguments == {"message": "hi"}
    payload = transport.calls[0]["payload"]
    assert payload["tools"][0]["function"]["name"] == "echo"
    assert "format" not in payload
    assert payload["think"] is False
    assert payload["options"]["num_predict"] == 256
    assert payload["stream"] is False


def test_ollama_caps_generated_tokens() -> None:
    transport = FakeTransport([JsonResponse(200, {"message": {"role": "assistant", "content": "Hi."}})])
    provider = OllamaChatProvider(
        "http://127.0.0.1:11434", "test", max_output_tokens=96, transport=transport
    )

    asyncio.run(provider.chat([ChatMessage(role="user", content="hello")], []))

    assert transport.calls[0]["payload"]["options"]["num_predict"] == 96


def test_ollama_sends_assistant_tool_calls_and_named_tool_results() -> None:
    transport = FakeTransport(
        [JsonResponse(200, {"message": {"role": "assistant", "content": "Done."}})]
    )
    provider = OllamaChatProvider("http://127.0.0.1:11434", "test", transport=transport)
    messages = [
        ChatMessage(
            role="assistant",
            tool_calls=[
                NativeToolCall(
                    function=NativeFunctionCall(name="echo", arguments={"message": "hi"})
                )
            ],
        ),
        ChatMessage(role="tool", name="echo", content='{"ok":true}'),
    ]

    response = asyncio.run(provider.chat(messages, tools()))

    assert response.message.content == "Done."
    sent = transport.calls[0]["payload"]["messages"]
    assert sent[-2]["tool_calls"][0]["function"]["name"] == "echo"
    assert sent[-1] == {"role": "tool", "content": '{"ok":true}', "tool_name": "echo"}


def test_openai_compatible_uses_native_function_calling_and_bearer_key() -> None:
    transport = FakeTransport(
        [
            JsonResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "abc",
                                        "type": "function",
                                        "function": {
                                            "name": "echo",
                                            "arguments": '{"message":"hi"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        ]
    )
    provider = OpenAICompatibleChatProvider(
        "http://127.0.0.1:8000/v1",
        "test",
        api_key="secret",
        transport=transport,
    )

    response = asyncio.run(provider.chat([ChatMessage(role="user", content="echo")], tools()))

    assert response.message.tool_calls[0].id == "abc"
    call = transport.calls[0]
    assert call["headers"] == {"Authorization": "Bearer secret"}
    assert call["payload"]["tools"][0]["type"] == "function"
    assert "response_format" not in call["payload"]


def test_llama_cpp_uses_openai_compatible_native_tools() -> None:
    transport = FakeTransport([JsonResponse(200, {"choices": [{"message": {"content": "Okay."}}]})])
    provider = LlamaCppChatProvider("http://127.0.0.1:8080/v1", "local", transport=transport)
    asyncio.run(provider.chat([ChatMessage(role="user", content="hello")], tools()))
    assert transport.calls[0]["payload"]["tools"]


def test_diagnostics_report_connection_failure_without_raising() -> None:
    transport = FakeTransport(error=HttpTransportError("connection refused"))
    provider = OllamaChatProvider("http://127.0.0.1:11434", "test", transport=transport)
    diagnostic = asyncio.run(provider.diagnose())
    assert diagnostic.available is False
    assert "connection refused" in diagnostic.message


def test_ollama_diagnostics_report_model_presence() -> None:
    transport = FakeTransport([JsonResponse(200, {"models": [{"name": "test"}]})])
    provider = OllamaChatProvider("http://127.0.0.1:11434", "test", transport=transport)
    diagnostic = asyncio.run(provider.diagnose())
    assert diagnostic.available is True


def test_ollama_warm_up_loads_model_without_generating_text() -> None:
    transport = FakeTransport([JsonResponse(200, {"done": True})])
    provider = OllamaChatProvider(
        "http://127.0.0.1:11434",
        "test",
        keep_alive="45m",
        transport=transport,
    )

    asyncio.run(provider.warm_up())

    call = transport.calls[0]
    assert call["url"] == "http://127.0.0.1:11434/api/generate"
    assert call["payload"] == {
        "model": "test",
        "prompt": "",
        "stream": False,
        "keep_alive": "45m",
    }


def test_openai_compatible_warm_up_forces_one_token_completion() -> None:
    transport = FakeTransport([JsonResponse(200, {"choices": []})])
    provider = OpenAICompatibleChatProvider(
        "http://127.0.0.1:8000/v1",
        "test",
        api_key="secret",
        transport=transport,
    )

    asyncio.run(provider.warm_up())

    call = transport.calls[0]
    assert call["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert call["payload"]["max_tokens"] == 1
    assert call["headers"] == {"Authorization": "Bearer secret"}


def test_ollama_retries_once_after_qwen_tool_xml_parser_500() -> None:
    transport = FlakyToolParseTransport()
    provider = OllamaChatProvider(
        "http://127.0.0.1:11434",
        "qwen3.5:4b",
        temperature=0.1,
        think=True,
        transport=transport,
    )

    response = asyncio.run(provider.chat([ChatMessage(role="user", content="echo")], tools()))

    assert response.message.tool_calls[0].function.arguments == {"message": "recovered"}
    assert len(transport.calls) == 2
    retry = transport.calls[1]["payload"]
    assert retry["think"] is False
    assert retry["options"]["temperature"] == 0.0
    assert "Tool-call recovery" in retry["messages"][0]["content"]
