from wyzer.brain import (
    FakeChatProvider,
    LlamaCppChatProvider,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
    create_chat_provider,
)
from wyzer.config import LLMSettings


def test_factory_selects_each_native_chat_provider() -> None:
    assert isinstance(create_chat_provider(LLMSettings()), FakeChatProvider)
    ollama = create_chat_provider(LLMSettings(provider="ollama", model="model"))
    compatible = create_chat_provider(LLMSettings(provider="openai_compatible", model="model"))
    llama = create_chat_provider(LLMSettings(provider="llama_cpp", model="model"))
    assert isinstance(ollama, OllamaChatProvider)
    assert isinstance(compatible, OpenAICompatibleChatProvider)
    assert isinstance(llama, LlamaCppChatProvider)
    assert ollama.endpoint == "http://127.0.0.1:11434"
    assert compatible.endpoint == "http://127.0.0.1:8000/v1"
    assert llama.endpoint == "http://127.0.0.1:8080/v1"
