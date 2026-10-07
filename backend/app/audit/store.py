"""Small SQLite audit ledger with idempotent, fail-closed writes."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock

from app.audit.models import AuditEvent, AuditEventType


class AuditStoreError(RuntimeError):
    """Base error for durable audit failures."""


class AuditStoreUnavailable(AuditStoreError):
    """Persistence cannot be safely used at a required boundary."""


class AuditStoreConflict(AuditStoreError):
    """The same deterministic event identity has different content."""


class AuditStoreCapacity(AuditStoreError):
    """The configured immutable retention limit has been reached."""


class AuditStore:
    """Thread-safe append-only ledger; capacity fails closed rather than pruning."""

    def __init__(self, path: Path, *, max_events: int = 100_000, timeout_seconds: float = 2.0) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("audit timeout must be between 0 and 30 seconds")
        self._path = path
        self._max_events = max_events
        self._timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._available = True
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def is_available(self) -> bool:
        return self._available

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT fingerprint FROM audit_events WHERE event_id = ?", (event.event_id,)
                ).fetchone()
                if existing is not None:
                    if existing[0] != event.fingerprint:
                        raise AuditStoreConflict("conflicting immutable audit event")
                    connection.commit()
                    return event
                count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
                if count >= self._max_events:
                    raise AuditStoreCapacity("audit ledger capacity reached")
                connection.execute(
                    """INSERT INTO audit_events (
                        event_id, fingerprint, event_type, occurred_at, execution_id, proposal_id,
                        intent_id, approval_id, account_reference, state, reason_code,
                        provenance_fingerprint, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id, event.fingerprint, event.event_type.value,
                        event.occurred_at.isoformat(), event.execution_id, event.proposal_id,
                        event.intent_id, event.approval_id, event.account_reference, event.state,
                        event.reason_code, event.provenance_fingerprint,
                        json.dumps(event.details, separators=(",", ":")),
                    ),
                )
                connection.commit()
                return event
            except AuditStoreError:
                connection.rollback()
                raise
            except (sqlite3.DatabaseError, OSError) as error:
                connection.rollback()
                self._available = False
                raise AuditStoreUnavailable("audit persistence unavailable") from error
            finally:
                connection.close()

    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """SELECT event_type, occurred_at, execution_id, proposal_id, intent_id,
                    approval_id, account_reference, state, reason_code, provenance_fingerprint,
                    details_json FROM audit_events ORDER BY occurred_at, event_id"""
                ).fetchall()
                events = tuple(
                    AuditEvent(
                        event_type=AuditEventType(row[0]), occurred_at=datetime.fromisoformat(row[1]),
                        execution_id=row[2], proposal_id=row[3], intent_id=row[4], approval_id=row[5],
                        account_reference=row[6], state=row[7], reason_code=row[8],
                        provenance_fingerprint=row[9], details=tuple(tuple(item) for item in json.loads(row[10])),
                    )
                    for row in rows
                )
            except (sqlite3.DatabaseError, ValueError, TypeError, json.JSONDecodeError) as error:
                self._available = False
                raise AuditStoreUnavailable("audit persistence unavailable") from error
            finally:
                connection.close()
        return events

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    execution_id TEXT,
                    proposal_id TEXT,
                    intent_id TEXT,
                    approval_id TEXT,
                    account_reference TEXT,
                    state TEXT,
                    reason_code TEXT,
                    provenance_fingerprint TEXT,
                    details_json TEXT NOT NULL
                    )"""
                )
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.DatabaseError, OSError) as error:
            self._available = False
            raise AuditStoreUnavailable("audit persistence unavailable") from error

    def _connect(self) -> sqlite3.Connection:
        if not self._available:
            raise AuditStoreUnavailable("audit persistence unavailable")
        return sqlite3.connect(self._path, timeout=self._timeout_seconds, isolation_level=None)
