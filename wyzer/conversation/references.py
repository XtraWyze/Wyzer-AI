"""Conservative conversational-reference resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wyzer.models import ConversationState, WorldStateSnapshot


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    resolved: bool
    arguments: dict[str, Any]
    question: str | None = None
    description: str | None = None


class ReferenceResolver:
    _WINDOW_REFERENCE = re.compile(r"\b(this|that|it|the current)\s+window\b|\b(this|that)\b", re.I)

    def window_arguments(
        self,
        text: str,
        world: WorldStateSnapshot,
        conversation: ConversationState,
    ) -> ReferenceResolution:
        if not self._WINDOW_REFERENCE.search(text):
            return ReferenceResolution(True, {})
        if world.foreground_window is not None:
            window = world.foreground_window
            return ReferenceResolution(
                True,
                {"window_handle": window.handle},
                description=window.title or window.application or "the focused window",
            )
        if len(conversation.recently_referenced_windows) == 1:
            window = conversation.recently_referenced_windows[-1]
            return ReferenceResolution(
                True,
                {"window_handle": window.handle},
                description=window.title or window.application or "the recent window",
            )
        return ReferenceResolution(
            False,
            {},
            question="Which window do you mean? Please focus it or name it.",
        )
