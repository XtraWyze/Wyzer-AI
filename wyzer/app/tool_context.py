"""Compact deterministic tool results for model context."""

from __future__ import annotations

import json
from typing import Any

from wyzer.models import ToolResult

_OMIT_KEYS = {
    "evidence",
    "screenshot_evidence",
    "screenshot_path",
    "path_sha256",
    "sha256",
    "image",
    "binary",
    "controls",
    "monitor_id",
}


class ToolResultContextBuilder:
    def __init__(self, maximum_characters: int = 4_000) -> None:
        if maximum_characters < 256:
            raise ValueError("tool-result context limit must be at least 256 characters")
        self._maximum_characters = maximum_characters

    def build(self, result: ToolResult) -> str:
        payload: dict[str, Any] = {"ok": result.ok, "tool": result.tool}
        if result.ok:
            payload["data"] = self._clean(self._response_data(result))
            payload["warnings"] = result.warnings[:8]
        else:
            assert result.error is not None
            payload["error"] = {
                "code": result.error.code,
                "message": result.error.message,
            }
            if result.error.code == "INVALID_TASK_ARGUMENTS":
                payload["error"]["recovery"] = (
                    "Do not expose this schema error or ask the user for planning fields. Reassess "
                    "the original request. Use a matching direct tool for one count, list, lookup, "
                    "or open. Only retry task planning for genuinely complex work, and author every "
                    "required goal, step description, and success criterion yourself."
                )
            elif result.error.details:
                payload["error"]["details"] = self._clean(result.error.details)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(serialized) <= self._maximum_characters:
            return serialized
        fallback = {
            "ok": result.ok,
            "tool": result.tool,
            "truncated": True,
            "summary": serialized[: self._maximum_characters - 100],
        }
        return json.dumps(fallback, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _response_data(result: ToolResult) -> dict[str, Any]:
        """Keep stable app identity prominent in model-visible action results."""
        data = dict(result.data or {})
        stable_target: str | None = None
        if result.tool == "open_application":
            application = data.get("application")
            if isinstance(application, str) and application.strip():
                stable_target = application.strip()
        elif result.tool in {"control_named_window", "move_named_window_to_monitor"}:
            target = data.get("target")
            if isinstance(target, str) and target.strip():
                stable_target = target.strip()

        if stable_target is None:
            return data

        data["response_target"] = stable_target
        raw_window = data.get("window")
        if isinstance(raw_window, dict):
            window = dict(raw_window)
            window.pop("title", None)
            data["window"] = window
        return data

    def _clean(self, value: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return "[truncated]"
        if isinstance(value, dict):
            return {
                str(key): self._clean(item, depth + 1)
                for key, item in list(value.items())[:40]
                if str(key).casefold() not in _OMIT_KEYS
            }
        if isinstance(value, list):
            return [self._clean(item, depth + 1) for item in value[:20]]
        if isinstance(value, str) and len(value) > 1_000:
            return value[:1_000] + "…"
        return value
