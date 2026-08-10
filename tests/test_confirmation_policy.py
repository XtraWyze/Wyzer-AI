import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tests.fake_windows import FakeWindowsBackend
from tests.fakes import ConsequentialEchoTool, text_response, tool_response
from wyzer.app import Orchestrator
from wyzer.brain import FakeChatProvider
from wyzer.models import ConfirmationMode
from wyzer.policy import ConfirmationPolicy
from wyzer.tools import ToolRegistry, create_default_registry
from wyzer.workers import InProcessExecutor


def build_confirmation_assistant() -> tuple[Orchestrator, FakeChatProvider]:
    registry = ToolRegistry()
    registry.register(ConsequentialEchoTool())
    provider = FakeChatProvider(
        [tool_response(("send_message", {"message": "hello"})), text_response("Sent.")]
    )
    return Orchestrator(registry, InProcessExecutor(registry), provider), provider


def test_policy_binds_confirmation_to_exact_validated_call_and_expiry() -> None:
    policy = ConfirmationPolicy()
    pending = policy.issue(uuid4(), uuid4(), "send_message", {"message": "hello"})
    assert policy.validate(pending, "send_message", {"message": "hello"}) == (
        True,
        "confirmed",
    )
    assert policy.validate(pending, "send_message", {"message": "changed"}) == (
        False,
        "arguments changed",
    )
    expired = pending.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert policy.validate(expired, "send_message", {"message": "hello"}) == (
        False,
        "expired",
    )


def test_consequential_action_uses_natural_yes_and_executes_stored_call() -> None:
    assistant, provider = build_confirmation_assistant()

    async def scenario() -> tuple[str, str]:
        requested = await assistant.handle("Send it")
        assert assistant.world.snapshot().recent_tool_calls == []
        completed = await assistant.handle("go ahead")
        return requested.text, completed.text

    requested, completed = asyncio.run(scenario())
    assert requested.endswith("Should I continue?")
    assert "token" not in requested.casefold()
    assert completed == "Sent."
    assert len(provider.requests) == 2
    result = assistant.world.snapshot().recent_tool_calls[-1]
    assert result.data == {"echoed": "hello"}


def test_no_cancels_pending_action_without_provider_request() -> None:
    assistant, provider = build_confirmation_assistant()

    async def scenario() -> str:
        await assistant.handle("Send it")
        return (await assistant.handle("never mind")).text

    assert asyncio.run(scenario()) == "Okay, I cancelled it."
    assert len(provider.requests) == 1
    assert assistant.world.snapshot().recent_tool_calls == []
    assert assistant.world.snapshot().pending_confirmation is None


def test_expired_confirmation_cannot_execute() -> None:
    assistant, _ = build_confirmation_assistant()

    async def scenario() -> str:
        await assistant.handle("Send it")
        pending = assistant.world.snapshot().pending_confirmation
        assert pending is not None
        assistant.world.set_confirmation(
            pending.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        )
        return (await assistant.handle("yes")).text

    assert "expired" in asyncio.run(scenario())
    assert assistant.world.snapshot().recent_tool_calls == []


def test_routine_desktop_actions_do_not_require_confirmation() -> None:
    registry = create_default_registry(FakeWindowsBackend())
    never = {
        "open_application",
        "control_master_audio",
        "control_application_audio",
        "control_media",
    }
    assert all(
        registry.get(name).definition().confirmation == ConfirmationMode.NEVER for name in never
    )
    policy = ConfirmationPolicy()
    close = registry.get("control_named_window").definition()
    assert close.confirmation == ConfirmationMode.CONDITIONAL
    assert (
        policy.requires_confirmation(
            close, {"window": "Calculator", "action": "close", "all_matches": False}
        )
        is False
    )
    assert (
        policy.requires_confirmation(
            close, {"window": "Google Chrome", "action": "close", "all_matches": False}
        )
        is True
    )
    pending = policy.issue(
        uuid4(),
        uuid4(),
        "control_named_window",
        {"window": "Google Chrome", "action": "close", "all_matches": False},
    )
    assert "personal Chrome" in pending.prompt
