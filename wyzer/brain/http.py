"""Small injectable asynchronous JSON HTTP transport."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class JsonResponse:
    status: int
    data: dict[str, Any]


class JsonTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60,
    ) -> JsonResponse: ...


class HttpTransportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class UrllibJsonTransport:
    async def request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60,
    ) -> JsonResponse:
        return await asyncio.to_thread(
            self._request_sync, method, url, payload, headers or {}, timeout_seconds
        )

    @staticmethod
    def _request_sync(
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "application/json", **headers}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except HTTPError as error:
            raw_error = error.read().decode("utf-8", errors="replace")
            raise HttpTransportError(
                _safe_http_error(raw_error, error.code), status=error.code
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise HttpTransportError(
                f"Could not reach the local model endpoint: {error}"
            ) from error
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HttpTransportError("Local model endpoint returned invalid JSON.") from error
        if not isinstance(decoded, dict):
            raise HttpTransportError("Local model endpoint returned a non-object JSON response.")
        return JsonResponse(status=status, data=cast(dict[str, Any], decoded))


def _safe_http_error(raw: str, status: int) -> str:
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            message = decoded.get("error")
            if isinstance(message, dict):
                message = message.get("message")
            if isinstance(message, str):
                return f"Local model endpoint returned HTTP {status}: {message[:500]}"
    except json.JSONDecodeError:
        pass
    return f"Local model endpoint returned HTTP {status}."
