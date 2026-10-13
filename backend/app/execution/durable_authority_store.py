"""SQLite lifecycle owner for Phase 13A approval and demo-authorization state.

This internal, non-submitting store is intentionally not wired into Pilot,
adapters, MT5, API routes, or existing process-local execution flows.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal, TypeAlias

from app.audit.models import AuditEvent, AuditEventType
from app.audit.service import AuditService
from app.audit.store import AuditStoreError
from app.models.demo_authorization import ControlledDemoAuthorization
from app.models.durable_authority import (
    DurableApprovalState,
    DurableAuthorityStatus,
    DurableDemoAuthorizationState,
)
from app.models.proposals import SentinelDecision


_SCHEMA_VERSION = "1"
_KIND_APPROVAL = "approval"
_KIND_AUTHORIZATION = "demo_authorization"
_TERMINAL = frozenset(
    {
        DurableAuthorityStatus.CONSUMED,
        DurableAuthorityStatus.REVOKED,
        DurableAuthorityStatus.EXPIRED,
        DurableAuthorityStatus.RECONCILIATION_REQUIRED,
    }
)
_LEGAL_TRANSITIONS: dict[DurableAuthorityStatus, frozenset[DurableAuthorityStatus]] = {
    DurableAuthorityStatus.ACTIVE: frozenset(_TERMINAL),
    DurableAuthorityStatus.CONSUMED: frozenset(),
    DurableAuthorityStatus.REVOKED: frozenset(),
    DurableAuthorityStatus.EXPIRED: frozenset(),
    DurableAuthorityStatus.RECONCILIATION_REQUIRED: frozenset(),
}
DurableAuthorityState: TypeAlias = DurableApprovalState | DurableDemoAuthorizationState


class DurableAuthorityStoreError(RuntimeError):
    """A deterministic failure at the durable authority-state boundary."""


class DurableAuthorityUnavailable(DurableAuthorityStoreError):
    """State persistence or outbox projection cannot safely continue."""


class DurableAuthorityConflict(DurableAuthorityStoreError):
    """The same external identifier has conflicting immutable lineage."""


class DurableAuthorityStateError(DurableAuthorityStoreError):
    """A lifecycle operation is unavailable or unsafe in its current state."""


class DurableAuthorityStore:
    """Bounded SQLite owner for internal approval and authorization lifecycles.

    It has no authority over existing process-local registries until a future,
    explicitly reviewed integration makes this store their sole state owner.
    """

    _schema_lock = RLock()

    def __init__(self, path: Path, *, max_records: int = 1_000, timeout_seconds: float = 2.0) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("durable authority timeout must be between 0 and 30 seconds")
        self._path = path
        self._max_records = max_records
        self._timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._available = True
        self._fail_next_commit_for_test = False
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def is_available(self) -> bool:
        return self._available

    def register_approval(
        self, decision: SentinelDecision, *, now: datetime
    ) -> tuple[DurableApprovalState, bool]:
        now_utc = _aware(now)
        state = DurableApprovalState(decision=decision, registered_at=now_utc, updated_at=now_utc)
        if now_utc >= decision.expires_at:  # type: ignore[operator]
            raise DurableAuthorityStateError("DURABLE_APPROVAL_EXPIRED")
        return self._register(_KIND_APPROVAL, state, state.approval_id, now_utc)  # type: ignore[return-value]

    def issue_demo_authorization(
        self, authorization: ControlledDemoAuthorization, *, now: datetime
    ) -> tuple[DurableDemoAuthorizationState, bool]:
        now_utc = _aware(now)
        state = DurableDemoAuthorizationState(authorization=authorization, issued_at=now_utc, updated_at=now_utc)
        if now_utc >= authorization.expires_at:
            raise DurableAuthorityStateError("DURABLE_DEMO_AUTHORIZATION_EXPIRED")
        return self._register(_KIND_AUTHORIZATION, state, state.authorization_id, now_utc)  # type: ignore[return-value]

    def verify_approval(self, decision: SentinelDecision, *, now: datetime) -> DurableApprovalState:
        state = self._get_active(_KIND_APPROVAL, _approval_id(decision), now=now)
        if not isinstance(state, DurableApprovalState) or state.decision != decision:
            raise DurableAuthorityStateError("DURABLE_APPROVAL_LINEAGE_MISMATCH")
        return state

    def verify_demo_authorization(
        self, authorization: ControlledDemoAuthorization, *, now: datetime
    ) -> DurableDemoAuthorizationState:
        state = self._get_active(_KIND_AUTHORIZATION, authorization.authorization_id, now=now)
        if not isinstance(state, DurableDemoAuthorizationState) or state.authorization != authorization:
            raise DurableAuthorityStateError("DURABLE_DEMO_AUTHORIZATION_LINEAGE_MISMATCH")
        return state

    def consume_approval(self, decision: SentinelDecision, *, now: datetime) -> tuple[DurableApprovalState, bool]:
        return self._consume(_KIND_APPROVAL, _approval_id(decision), decision, now=now)  # type: ignore[return-value]

    def consume_demo_authorization(
        self, authorization: ControlledDemoAuthorization, *, now: datetime
    ) -> tuple[DurableDemoAuthorizationState, bool]:
        return self._consume(_KIND_AUTHORIZATION, authorization.authorization_id, authorization, now=now)  # type: ignore[return-value]

    def revoke_approval(self, approval_id: str, *, now: datetime) -> DurableApprovalState:
        return self._transition(_KIND_APPROVAL, approval_id, DurableAuthorityStatus.REVOKED, now=now, event_state="durable_approval_revoked")  # type: ignore[return-value]

    def revoke_demo_authorization(self, authorization_id: str, *, now: datetime) -> DurableDemoAuthorizationState:
        return self._transition(_KIND_AUTHORIZATION, authorization_id, DurableAuthorityStatus.REVOKED, now=now, event_state="durable_demo_authorization_revoked")  # type: ignore[return-value]

    def mark_reconciliation_required(
        self, *, kind: Literal["approval", "demo_authorization"], external_id: str, now: datetime
    ) -> DurableAuthorityState:
        return self._transition(kind, external_id, DurableAuthorityStatus.RECONCILIATION_REQUIRED, now=now, event_state="durable_authority_reconciliation_required")

    def get_approval(self, approval_id: str) -> DurableApprovalState | None:
        state = self._get(_KIND_APPROVAL, approval_id)
        return state if isinstance(state, DurableApprovalState) else None

    def get_demo_authorization(self, authorization_id: str) -> DurableDemoAuthorizationState | None:
        state = self._get(_KIND_AUTHORIZATION, authorization_id)
        return state if isinstance(state, DurableDemoAuthorizationState) else None

    def history(self, *, kind: Literal["approval", "demo_authorization"], external_id: str) -> tuple[tuple[int, str | None, str, str], ...]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT lifecycle_version, from_status, to_status, event_state FROM durable_authority_lifecycle WHERE kind = ? AND external_id = ? ORDER BY lifecycle_version",
                    (kind, _id(external_id)),
                ).fetchall()
                return tuple((int(row[0]), row[1], row[2], row[3]) for row in rows)
            except sqlite3.DatabaseError as error:
                self._available = False
                raise DurableAuthorityUnavailable("durable authority lifecycle unavailable") from error
            finally:
                connection.close()

    def project_outbox(self, audit_service: AuditService) -> int:
        """Project local events idempotently; no cross-database atomicity is claimed."""
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT outbox_id, event_json FROM durable_authority_outbox WHERE delivered_at IS NULL ORDER BY outbox_id"
                ).fetchall()
            except sqlite3.DatabaseError as error:
                self._available = False
                raise DurableAuthorityUnavailable("durable authority outbox unavailable") from error
            finally:
                connection.close()
        delivered = 0
        for outbox_id, event_json in rows:
            event = _audit_from_json(event_json)
            try:
                audit_service.record(event)
            except AuditStoreError as error:
                raise DurableAuthorityUnavailable("durable authority audit projection unavailable") from error
            with self._lock:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    changed = connection.execute(
                        "UPDATE durable_authority_outbox SET delivered_at = ? WHERE outbox_id = ? AND delivered_at IS NULL",
                        (event.occurred_at.isoformat(), outbox_id),
                    ).rowcount
                    self._commit(connection)
                    delivered += int(changed == 1)
                except (sqlite3.DatabaseError, OSError) as error:
                    connection.rollback()
                    self._available = False
                    raise DurableAuthorityUnavailable("durable authority outbox acknowledgement unavailable") from error
                finally:
                    connection.close()
        return delivered

    def recover(self, *, now: datetime) -> tuple[DurableAuthorityState, ...]:
        """Persist expiry for known active records; never recreate or retry state."""
        now_utc = _aware(now)
        expired: list[DurableAuthorityState] = []
        for kind, external_id in self._active_identifiers():
            state = self._get(kind, external_id)
            if state is not None and now_utc >= _expires_at(state):
                expired.append(self._transition(kind, external_id, DurableAuthorityStatus.EXPIRED, now=now_utc, event_state="durable_authority_expired"))
        return tuple(expired)

    def _register(self, kind: str, state: DurableAuthorityState, external_id: str, now: datetime) -> tuple[DurableAuthorityState, bool]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT payload_json FROM durable_authority_records WHERE kind = ? AND external_id = ?", (kind, external_id)
                ).fetchone()
                if existing is not None:
                    restored = _state_from_json(kind, existing[0])
                    if restored.source_fingerprint != state.source_fingerprint:
                        raise DurableAuthorityConflict("DURABLE_AUTHORITY_LINEAGE_CONFLICT")
                    connection.commit()
                    return restored, False
                count = connection.execute("SELECT COUNT(*) FROM durable_authority_records").fetchone()[0]
                if count >= self._max_records:
                    raise DurableAuthorityStateError("DURABLE_AUTHORITY_CAPACITY_REACHED")
                connection.execute(
                    "INSERT INTO durable_authority_records (record_id, kind, external_id, source_fingerprint, status, lifecycle_version, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (state.record_id, kind, external_id, state.source_fingerprint, state.status.value, state.lifecycle_version, _state_json(state)),
                )
                self._insert_lifecycle(connection, kind, external_id, state, None, f"durable_{kind}_registered", now)
                self._insert_outbox(connection, kind, external_id, state, f"durable_{kind}_registered", now)
                self._commit(connection)
                return state, True
            except DurableAuthorityStoreError:
                connection.rollback()
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                self._available = False
                raise DurableAuthorityUnavailable("durable authority persistence unavailable") from error
            finally:
                connection.close()

    def _get_active(self, kind: str, external_id: str, *, now: datetime) -> DurableAuthorityState:
        now_utc = _aware(now)
        state = self._get(kind, external_id)
        if state is None:
            raise DurableAuthorityStateError("DURABLE_AUTHORITY_NOT_FOUND")
        if state.status is DurableAuthorityStatus.ACTIVE and now_utc >= _expires_at(state):
            state = self._transition(kind, external_id, DurableAuthorityStatus.EXPIRED, now=now_utc, event_state="durable_authority_expired")
        if state.status is not DurableAuthorityStatus.ACTIVE:
            raise DurableAuthorityStateError(f"DURABLE_AUTHORITY_{state.status.value.upper()}")
        return state

    def _consume(self, kind: str, external_id: str, source: SentinelDecision | ControlledDemoAuthorization, *, now: datetime) -> tuple[DurableAuthorityState, bool]:
        now_utc = _aware(now)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT payload_json FROM durable_authority_records WHERE kind = ? AND external_id = ?", (kind, external_id)
                ).fetchone()
                if row is None:
                    raise DurableAuthorityStateError("DURABLE_AUTHORITY_NOT_FOUND")
                current = _state_from_json(kind, row[0])
                if current.source_fingerprint != _source_fingerprint(source):
                    raise DurableAuthorityStateError("DURABLE_AUTHORITY_LINEAGE_MISMATCH")
                if current.status is DurableAuthorityStatus.CONSUMED:
                    connection.commit()
                    return current, False
                if current.status is not DurableAuthorityStatus.ACTIVE:
                    raise DurableAuthorityStateError(f"DURABLE_AUTHORITY_{current.status.value.upper()}")
                if now_utc >= _expires_at(current):
                    updated = _updated(current, DurableAuthorityStatus.EXPIRED, now_utc)
                    self._persist_transition(connection, kind, external_id, current, updated, "durable_authority_expired", now_utc)
                    self._commit(connection)
                    raise DurableAuthorityStateError("DURABLE_AUTHORITY_EXPIRED")
                updated = _updated(current, DurableAuthorityStatus.CONSUMED, now_utc)
                self._persist_transition(connection, kind, external_id, current, updated, f"durable_{kind}_consumed", now_utc)
                self._commit(connection)
                return updated, True
            except DurableAuthorityStoreError:
                connection.rollback()
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                self._available = False
                raise DurableAuthorityUnavailable("durable authority consumption unavailable") from error
            finally:
                connection.close()

    def _transition(self, kind: str, external_id: str, status: DurableAuthorityStatus, *, now: datetime, event_state: str) -> DurableAuthorityState:
        now_utc = _aware(now)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT payload_json FROM durable_authority_records WHERE kind = ? AND external_id = ?", (kind, _id(external_id))).fetchone()
                if row is None:
                    raise DurableAuthorityStateError("DURABLE_AUTHORITY_NOT_FOUND")
                current = _state_from_json(kind, row[0])
                if current.status is status:
                    connection.commit()
                    return current
                if status not in _LEGAL_TRANSITIONS[current.status]:
                    raise DurableAuthorityStateError("DURABLE_AUTHORITY_LIFECYCLE_INVALID")
                updated = _updated(current, status, now_utc)
                self._persist_transition(connection, kind, _id(external_id), current, updated, event_state, now_utc)
                self._commit(connection)
                return updated
            except DurableAuthorityStoreError:
                connection.rollback()
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                self._available = False
                raise DurableAuthorityUnavailable("durable authority transition unavailable") from error
            finally:
                connection.close()

    def _persist_transition(self, connection: sqlite3.Connection, kind: str, external_id: str, current: DurableAuthorityState, updated: DurableAuthorityState, event_state: str, now: datetime) -> None:
        connection.execute(
            "UPDATE durable_authority_records SET status = ?, lifecycle_version = ?, payload_json = ? WHERE kind = ? AND external_id = ?",
            (updated.status.value, updated.lifecycle_version, _state_json(updated), kind, external_id),
        )
        self._insert_lifecycle(connection, kind, external_id, updated, current.status, event_state, now)
        self._insert_outbox(connection, kind, external_id, updated, event_state, now)

    def _get(self, kind: str, external_id: str) -> DurableAuthorityState | None:
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("SELECT payload_json FROM durable_authority_records WHERE kind = ? AND external_id = ?", (kind, _id(external_id))).fetchone()
                return _state_from_json(kind, row[0]) if row is not None else None
            except (sqlite3.DatabaseError, ValueError, json.JSONDecodeError) as error:
                self._available = False
                raise DurableAuthorityUnavailable("durable authority read unavailable") from error
            finally:
                connection.close()

    def _active_identifiers(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute("SELECT kind, external_id FROM durable_authority_records WHERE status = ? ORDER BY kind, external_id", (DurableAuthorityStatus.ACTIVE.value,)).fetchall()
                return tuple((row[0], row[1]) for row in rows)
            except sqlite3.DatabaseError as error:
                self._available = False
                raise DurableAuthorityUnavailable("durable authority read unavailable") from error
            finally:
                connection.close()

    def _insert_lifecycle(self, connection: sqlite3.Connection, kind: str, external_id: str, state: DurableAuthorityState, from_status: DurableAuthorityStatus | None, event_state: str, now: datetime) -> None:
        connection.execute(
            "INSERT INTO durable_authority_lifecycle (kind, external_id, lifecycle_version, from_status, to_status, event_state, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, external_id, state.lifecycle_version, None if from_status is None else from_status.value, state.status.value, event_state, now.isoformat()),
        )

    def _insert_outbox(self, connection: sqlite3.Connection, kind: str, external_id: str, state: DurableAuthorityState, event_state: str, now: datetime) -> None:
        event = AuditEvent(
            event_type=AuditEventType.SENTINEL_DECISION if kind == _KIND_APPROVAL else AuditEventType.DEMO_AUTHORIZATION,
            occurred_at=now,
            execution_id=state.record_id,
            proposal_id=_proposal_id(state),
            intent_id=_intent_id(state),
            approval_id=_approval_from_state(state),
            account_reference=_account_reference(state),
            state=event_state,
            reason_code="DURABLE_AUTHORITY_STATE",
            provenance_fingerprint=state.fingerprint,
            details=(("test_only", "true"), ("submission_allowed", "false")),
        )
        connection.execute(
            "INSERT INTO durable_authority_outbox (outbox_id, kind, external_id, event_json, delivered_at) VALUES (?, ?, ?, ?, NULL)",
            (f"{state.record_id}:{state.lifecycle_version}", kind, external_id, _audit_json(event)),
        )

    def _commit(self, connection: sqlite3.Connection) -> None:
        if self._fail_next_commit_for_test:
            self._fail_next_commit_for_test = False
            raise sqlite3.OperationalError("synthetic commit failure")
        connection.commit()

    def _initialize(self) -> None:
        try:
            with self._schema_lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self._path, timeout=self._timeout_seconds, isolation_level=None)
                try:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
                    required = {"durable_authority_schema_metadata", "durable_authority_records", "durable_authority_lifecycle", "durable_authority_outbox"}
                    if tables and not required.issubset(tables):
                        raise DurableAuthorityUnavailable("durable authority schema is missing or corrupt")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_authority_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_authority_records (record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL, status TEXT NOT NULL, lifecycle_version INTEGER NOT NULL, payload_json TEXT NOT NULL, UNIQUE(kind, external_id))")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_authority_lifecycle (kind TEXT NOT NULL, external_id TEXT NOT NULL, lifecycle_version INTEGER NOT NULL, from_status TEXT NULL, to_status TEXT NOT NULL, event_state TEXT NOT NULL, occurred_at TEXT NOT NULL, PRIMARY KEY(kind, external_id, lifecycle_version))")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_authority_outbox (outbox_id TEXT PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL, event_json TEXT NOT NULL, delivered_at TEXT NULL)")
                    version = connection.execute("SELECT value FROM durable_authority_schema_metadata WHERE key = 'schema_version'").fetchone()
                    if version is None:
                        connection.execute("INSERT INTO durable_authority_schema_metadata (key, value) VALUES ('schema_version', ?)", (_SCHEMA_VERSION,))
                    elif version[0] != _SCHEMA_VERSION:
                        raise DurableAuthorityUnavailable("unsupported durable authority schema version")
                    connection.commit()
                finally:
                    connection.close()
        except DurableAuthorityStoreError:
            self._available = False
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            self._available = False
            raise DurableAuthorityUnavailable("durable authority database unavailable") from error

    def _connect(self) -> sqlite3.Connection:
        if not self._available:
            raise DurableAuthorityUnavailable("durable authority database unavailable")
        return sqlite3.connect(self._path, timeout=self._timeout_seconds, isolation_level=None)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("durable authority timestamps require timezone awareness")
    return value.astimezone(UTC)


def _id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("durable authority identifier cannot be blank")
    return normalized


def _approval_id(decision: SentinelDecision) -> str:
    if not decision.approval_id:
        raise DurableAuthorityStateError("DURABLE_APPROVAL_ID_REQUIRED")
    return decision.approval_id


def _expires_at(state: DurableAuthorityState) -> datetime:
    return state.decision.expires_at if isinstance(state, DurableApprovalState) else state.authorization.expires_at  # type: ignore[return-value]


def _updated(state: DurableAuthorityState, status: DurableAuthorityStatus, now: datetime) -> DurableAuthorityState:
    if isinstance(state, DurableApprovalState):
        return state.model_copy(update={"status": status, "updated_at": now, "lifecycle_version": state.lifecycle_version + 1})
    return state.model_copy(update={"status": status, "updated_at": now, "lifecycle_version": state.lifecycle_version + 1})


def _source_fingerprint(source: SentinelDecision | ControlledDemoAuthorization) -> str:
    if isinstance(source, SentinelDecision):
        from app.models.durable_authority import _fingerprint
        return _fingerprint(source.model_dump(mode="json"))
    return source.fingerprint


def _state_json(state: DurableAuthorityState) -> str:
    data = state.model_dump(mode="json")
    for key in ("approval_id", "authorization_id", "record_id", "source_fingerprint", "fingerprint"):
        data.pop(key, None)
    if isinstance(state, DurableDemoAuthorizationState) and isinstance(data.get("authorization"), dict):
        data["authorization"].pop("authorization_id", None)
        data["authorization"].pop("fingerprint", None)
        account = data["authorization"].get("account")
        if isinstance(account, dict):
            # ``account_id`` is a computed field on the nested account key;
            # it is not accepted as input when the durable state is restored.
            account.pop("account_id", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _state_from_json(kind: str, value: str) -> DurableAuthorityState:
    data = json.loads(value)
    if kind == _KIND_APPROVAL:
        return DurableApprovalState.model_validate(data)
    if kind == _KIND_AUTHORIZATION:
        return DurableDemoAuthorizationState.model_validate(data)
    raise ValueError("unknown durable authority kind")


def _audit_json(event: AuditEvent) -> str:
    data = event.model_dump(mode="json")
    data.pop("event_id", None)
    data.pop("fingerprint", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _audit_from_json(value: str) -> AuditEvent:
    return AuditEvent.model_validate(json.loads(value))


def _proposal_id(state: DurableAuthorityState) -> str:
    return state.decision.proposal_id if isinstance(state, DurableApprovalState) else state.authorization.proposal_id


def _intent_id(state: DurableAuthorityState) -> str | None:
    return None if isinstance(state, DurableApprovalState) else state.authorization.execution_intent_id


def _approval_from_state(state: DurableAuthorityState) -> str:
    return state.approval_id if isinstance(state, DurableApprovalState) else state.authorization.approval_id


def _account_reference(state: DurableAuthorityState) -> str | None:
    if isinstance(state, DurableApprovalState):
        return state.decision.account_id
    return state.authorization.account.account_reference
