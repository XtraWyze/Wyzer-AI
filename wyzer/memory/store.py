"""Small consent-first SQLite store for explicitly requested personal memories."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from wyzer.models import ConsentStatus, MemoryRecord

_SENSITIVE = re.compile(
    r"\b(?:password|passcode|pin|api[ -]?key|access[ -]?token|secret|private key|"
    r"credit card|debit card|bank account|routing number|social security|ssn|"
    r"medical|diagnosis|medication|private message|clipboard)\b",
    re.I,
)


class SensitiveMemoryError(ValueError):
    """Raised when a requested memory is too sensitive to persist."""


class MemoryStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def remember(self, fact: str) -> MemoryRecord:
        clean = " ".join(fact.strip().split())
        if not clean:
            raise ValueError("memory cannot be empty")
        if _SENSITIVE.search(clean):
            raise SensitiveMemoryError("that information is too sensitive to store")
        category = self._category(clean)
        record = MemoryRecord(
            category=category,
            content={"fact": clean},
            source="explicit user request",
            sensitivity="normal",
            confidence=1.0,
            consent_status=ConsentStatus.GRANTED,
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT memory_id FROM memories WHERE lower(fact) = lower(?)", (clean,)
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE memories SET updated_at = ? WHERE memory_id = ?",
                    (record.updated_at.isoformat(), existing[0]),
                )
                found = self._get(connection, str(existing[0]))
                if found is not None:
                    return found
            connection.execute(
                """
                INSERT INTO memories (
                    memory_id, category, fact, created_at, updated_at, source,
                    sensitivity, confidence, consent_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.memory_id),
                    record.category,
                    clean,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.source,
                    record.sensitivity,
                    record.confidence,
                    record.consent_status.value,
                ),
            )
        return record

    def list(self, limit: int = 100) -> list[MemoryRecord]:
        bounded = min(max(limit, 1), 500)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def forget(self, query: str) -> int:
        clean = " ".join(query.strip().split())
        if not clean:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE instr(lower(fact), lower(?)) > 0", (clean,)
            )
            return cursor.rowcount

    def clear(self) -> int:
        with self._connect() as connection:
            count = int(connection.execute("SELECT count(*) FROM memories").fetchone()[0])
            connection.execute("DELETE FROM memories")
            return count

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    consent_status TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS memories_fact_lower ON memories(fact COLLATE NOCASE)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _get(connection: sqlite3.Connection, memory_id: str) -> MemoryRecord | None:
        row = connection.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        return MemoryStore._from_row(row) if row is not None else None

    @staticmethod
    def _from_row(row: Any) -> MemoryRecord:
        return MemoryRecord(
            memory_id=UUID(str(row["memory_id"])),
            category=str(row["category"]),
            content={"fact": str(row["fact"])},
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            source=str(row["source"]),
            sensitivity=str(row["sensitivity"]),
            confidence=float(row["confidence"]),
            consent_status=ConsentStatus(str(row["consent_status"])),
        )

    @staticmethod
    def _category(fact: str) -> str:
        lowered = fact.casefold()
        if "my name" in lowered or lowered.startswith("name is"):
            return "identity"
        if "prefer" in lowered or "favorite" in lowered or "favourite" in lowered:
            return "preference"
        if "project" in lowered or "working on" in lowered:
            return "project"
        return "user_fact"
