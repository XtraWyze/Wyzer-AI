"""Small synchronous Ollama vision client used inside tool workers."""

from __future__ import annotations

import base64
import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class VisionClientError(RuntimeError):
    pass


class VisionClient(Protocol):
    available: bool
    unavailable_reason: str | None
    model: str

    def analyze(self, image_bytes: bytes, prompt: str) -> dict[str, Any]: ...
    def locate(self, image_bytes: bytes, target: str) -> dict[str, Any]: ...


_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "visible_text": {"type": "array", "items": {"type": "string"}},
        "relevant_elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "kind": {"type": "string"},
                    "state": {"type": ["string", "null"]},
                },
                "required": ["label", "kind", "state"],
                "additionalProperties": False,
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "visible_text", "relevant_elements", "warnings"],
    "additionalProperties": False,
}

_LOCATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "x": {"type": ["integer", "null"], "minimum": 0, "maximum": 1000},
        "y": {"type": ["integer", "null"], "minimum": 0, "maximum": 1000},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "description": {"type": "string"},
    },
    "required": ["found", "x", "y", "confidence", "description"],
    "additionalProperties": False,
}


class OllamaVisionClient:
    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        timeout_seconds: float = 45,
        temperature: float = 0.0,
        think: bool = False,
        keep_alive: str = "30m",
        enabled: bool = True,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.think = think
        self.keep_alive = keep_alive
        self.available = bool(enabled and self.endpoint and self.model)
        self.unavailable_reason = (
            None if self.available else "vision requires an enabled Ollama model"
        )

    def analyze(self, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        instruction = (
            "You are Wyzer's screen-perception module. Analyze only what is visibly present in "
            "the screenshot. Do not assume hidden state. Be concise. Return the requested JSON. "
            "The user's question is: " + prompt
        )
        return self._request(image_bytes, instruction, _ANALYSIS_SCHEMA)

    def locate(self, image_bytes: bytes, target: str) -> dict[str, Any]:
        instruction = (
            "Locate the single visible UI target described below. Return a point near the center "
            "of the clickable target using normalized image coordinates from 0 to 1000 for x and "
            "y. If the target is missing, ambiguous, covered, or you are not confident, set found "
            "to false and x/y to null. Never guess. Target: " + target
        )
        return self._request(image_bytes, instruction, _LOCATION_SCHEMA)

    def _request(
        self,
        image_bytes: bytes,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.available:
            raise VisionClientError(self.unavailable_reason or "vision is unavailable")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(image_bytes).decode("ascii")],
                }
            ],
            "format": response_schema,
            "stream": False,
            "think": self.think,
            "options": {"temperature": self.temperature},
            "keep_alive": self.keep_alive,
        }
        request = Request(
            f"{self.endpoint}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise VisionClientError(
                f"Ollama vision request failed with HTTP {error.code}: {body[:300]}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise VisionClientError(
                f"Could not reach Ollama for screen perception: {error}"
            ) from error

        try:
            decoded = json.loads(raw)
            message = decoded.get("message") if isinstance(decoded, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            result = json.loads(content) if isinstance(content, str) else None
        except json.JSONDecodeError as error:
            raise VisionClientError("Ollama returned invalid JSON for screen perception") from error
        if not isinstance(result, dict):
            raise VisionClientError("Ollama returned no structured screen-perception result")
        return result
