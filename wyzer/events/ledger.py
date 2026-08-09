"""Thread-safe bounded in-memory event ledger."""

from __future__ import annotations

from collections import deque
from threading import RLock

from wyzer.models import EventKind, EventRecord


class EventLedger:
    def __init__(self, capacity: int = 500) -> None:
        if capacity < 1:
            raise ValueError("event ledger capacity must be positive")
        self._events: deque[EventRecord] = deque(maxlen=capacity)
        self._lock = RLock()

    @property
    def capacity(self) -> int:
        maxlen = self._events.maxlen
        assert maxlen is not None
        return maxlen

    def append(self, event: EventRecord) -> None:
        with self._lock:
            self._events.append(event)

    def recent(self, limit: int = 50, kind: EventKind | None = None) -> tuple[EventRecord, ...]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        with self._lock:
            values = tuple(self._events)
        if kind is not None:
            values = tuple(event for event in values if event.kind == kind)
        return values[-limit:] if limit else ()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
