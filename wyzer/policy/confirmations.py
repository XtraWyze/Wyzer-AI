"""Small deterministic confirmation policy bound to an exact validated tool call."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from wyzer.models import ConfirmationMode, PendingConfirmation, ToolDefinition

_CONSEQUENTIAL = re.compile(
    r"\b(send|submit|buy|purchase|pay|delete permanently|empty (?:the )?trash|install|"
    r"uninstall|restart|shutdown|shut down|log ?off|sign ?out)\b",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"\b(password|passcode|pin|credential|security code|one[- ]time code|otp)\b",
    re.IGNORECASE,
)


class ConfirmationPolicy:
    def __init__(self, ttl_seconds: float = 120) -> None:
        if ttl_seconds <= 0:
            raise ValueError("confirmation TTL must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)

    def requires_confirmation(self, definition: ToolDefinition, arguments: dict[str, Any]) -> bool:
        if definition.confirmation == ConfirmationMode.ALWAYS:
            return True
        if definition.confirmation == ConfirmationMode.NEVER:
            return False
        if definition.name == "write_text_file":
            return arguments.get("overwrite") is True
        if (
            definition.name == "control_named_window"
            and arguments.get("action") == "close"
            and "chrome" in str(arguments.get("window", "")).casefold()
        ):
            return True
        searchable = " ".join(
            str(arguments.get(key, ""))
            for key in (
                "query",
                "selector",
                "control",
                "control_label",
                "label",
                "action",
                "field",
            )
        )
        if _CONSEQUENTIAL.search(searchable):
            return True
        return arguments.get("action") == "set_value" and bool(_CREDENTIAL.search(searchable))

    @staticmethod
    def digest(tool_name: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def issue(
        self,
        action_id: UUID,
        step_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        provider_call_id: str | None = None,
        prompt_arguments: dict[str, Any] | None = None,
    ) -> PendingConfirmation:
        return PendingConfirmation(
            action_id=action_id,
            step_id=step_id,
            tool_name=tool_name,
            arguments=arguments,
            provider_call_id=provider_call_id,
            prompt=self._prompt(tool_name, prompt_arguments or arguments),
            expires_at=datetime.now(UTC) + self._ttl,
            call_digest=self.digest(tool_name, arguments),
        )

    def validate(
        self,
        pending: PendingConfirmation,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[bool, str]:
        if datetime.now(UTC) >= pending.expires_at:
            return False, "expired"
        if pending.tool_name != tool_name:
            return False, "tool changed"
        digest = self.digest(tool_name, arguments)
        if not hmac.compare_digest(pending.call_digest, digest):
            return False, "arguments changed"
        return True, "confirmed"

    @staticmethod
    def _prompt(tool_name: str, arguments: dict[str, Any]) -> str:
        if (
            tool_name == "control_named_window"
            and arguments.get("action") == "close"
            and "chrome" in str(arguments.get("window", "")).casefold()
        ):
            return (
                "This will close your personal Chrome window, not Wyzer's managed browser. "
                "Should I continue?"
            )
        target = next(
            (
                str(arguments[key])
                for key in (
                    "query",
                    "control_label",
                    "label",
                    "recipient",
                    "target",
                    "application",
                    "window",
                    "path",
                    "source",
                    "destination",
                )
                if arguments.get(key)
            ),
            "this action",
        )
        if tool_name == "delete_path":
            return f"This will move {target} to the Recycle Bin. Should I continue?"
        if tool_name == "write_text_file" and arguments.get("overwrite") is True:
            return f"This will replace the existing text file {target}. Should I continue?"
        if "send" in target.casefold() or tool_name.startswith("send_"):
            return f"This will send {target}. Should I continue?"
        if arguments.get("action") == "set_value" and _CREDENTIAL.search(target):
            return f"This will enter credentials into {target}. Should I continue?"
        return f"This will perform the consequential action '{target}'. Should I continue?"
