"""SQLite-backed, non-submitting durable reservation and audit-outbox store.

This Phase 13 module is intentionally isolated from Pilot, adapters, MT5, API
routes, and production execution state.  It records a test-only reservation;
it cannot authorize a broker operation.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from abc import ABC, abstractmethod
from typing import Literal

from app.audit.models import AuditEvent, AuditEventType
from app.audit.service import AuditService
from app.audit.store import AuditStoreError
from app.models.durable_execution import (
    DurableAuthorizationConsumptionState,
    DurableExecutionReservation,
    DurableTestOnlyReservationEvaluation,
    DurableReservationStatus,
)


_SCHEMA_VERSION = "2"
_TERMINAL = frozenset({
    DurableReservationStatus.REJECTED_TERMINAL,
    DurableReservationStatus.COMPLETED_TERMINAL,
    DurableReservationStatus.REVOKED,
    DurableReservationStatus.EXPIRED,
    DurableReservationStatus.RECONCILIATION_REQUIRED,
})
_LEGAL_TRANSITIONS: dict[DurableReservationStatus, frozenset[DurableReservationStatus]] = {
    DurableReservationStatus.RESERVED: frozenset({
        DurableReservationStatus.READY_FOR_ATTEMPT,
        DurableReservationStatus.RECONCILIATION_REQUIRED,
        DurableReservationStatus.REVOKED,
        DurableReservationStatus.EXPIRED,
    }),
    DurableReservationStatus.READY_FOR_ATTEMPT: frozenset({
        DurableReservationStatus.ATTEMPT_STARTED,
        DurableReservationStatus.RECONCILIATION_REQUIRED,
        DurableReservationStatus.REVOKED,
        DurableReservationStatus.EXPIRED,
    }),
    DurableReservationStatus.ATTEMPT_STARTED: frozenset({
        DurableReservationStatus.REJECTED_TERMINAL,
        DurableReservationStatus.COMPLETED_TERMINAL,
        DurableReservationStatus.RECONCILIATION_REQUIRED,
    }),
    DurableReservationStatus.RECONCILIATION_REQUIRED: frozenset(),
    DurableReservationStatus.REJECTED_TERMINAL: frozenset(),
    DurableReservationStatus.COMPLETED_TERMINAL: frozenset(),
    DurableReservationStatus.REVOKED: frozenset(),
    DurableReservationStatus.EXPIRED: frozenset(),
}


class DurableReservationStoreError(RuntimeError):
    """A deterministic failure at the durable reservation boundary."""


class DurableReservationUnavailable(DurableReservationStoreError):
    """SQLite or the mandatory audit projection cannot prove safe state."""


class DurableReservationConflict(DurableReservationStoreError):
    """A first-order identity has different immutable bound evidence."""


class DurableReservationStateError(DurableReservationStoreError):
    """A lifecycle operation is unavailable or unsafe in the current state."""


class DurableTestOnlySafetyEvidenceEvaluator(ABC):
    """Explicit synthetic evaluator for isolated Phase 13 tests.

    This interface is not a trust boundary.  An arbitrary Python caller can
    implement it, so its result is accepted only as a test-only structural
    evaluation and can never create production authorization or submission
    authority.  No authoritative Phase 13 evaluator exists yet.
    """

    test_only: Literal[True] = True

    @abstractmethod
    def verify(
        self, reservation: DurableExecutionReservation, *, now: datetime
    ) -> DurableTestOnlyReservationEvaluation:
        """Return complete synthetic checks for this exact test reservation."""


class DurableReservationStore:
    """Exactly one durable test-only reservation with local atomic outbox writes.

    Reservation/lifecycle rows and outbox rows share one SQLite transaction.
    The project audit ledger can be a separate database, so projection is
    idempotent and explicitly not cross-store atomic.  Existing authorization
    consumption remains process-local and is never consumed by this class.
    """

    _schema_initialization_lock = RLock()

    def __init__(
        self,
        path: Path,
        *,
        test_only_evaluator: DurableTestOnlySafetyEvidenceEvaluator | None = None,
        max_records: int = 1,
        timeout_seconds: float = 2.0,
    ) -> None:
        if max_records != 1:
            raise ValueError("Phase 13 permits exactly one durable first-demo reservation")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("durable reservation timeout must be between 0 and 30 seconds")
        self._path = path
        self._max_records = max_records
        self._timeout_seconds = timeout_seconds
        if test_only_evaluator is None:
            raise DurableReservationUnavailable("DURABLE_TEST_EVALUATOR_REQUIRED")
        if not isinstance(test_only_evaluator, DurableTestOnlySafetyEvidenceEvaluator):
            raise TypeError("durable reservation evaluator must use the explicit test-only evaluator contract")
        if test_only_evaluator.test_only is not True:
            raise DurableReservationUnavailable("DURABLE_NON_TEST_EVALUATOR_REJECTED")
        self._test_only_evaluator = test_only_evaluator
        self._available = True
        self._lock = RLock()
        self._fail_next_commit_for_test = False
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def test_only(self) -> Literal[True]:
        """This isolated store can never become a production authorization."""
        return True

    def is_available(self) -> bool:
        return self._available

    def reserve(self, reservation: DurableExecutionReservation) -> tuple[DurableExecutionReservation, bool]:
        """Atomically persist a validated reservation and creation-audit outbox row."""
        if reservation.status is not DurableReservationStatus.RESERVED:
            raise DurableReservationStateError("DURABLE_RESERVATION_MUST_BEGIN_RESERVED")
        self._validate_test_only_evaluation(reservation, now=reservation.created_at)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT payload_json FROM durable_reservations WHERE execution_identity = ?",
                    (reservation.execution_identity,),
                ).fetchone()
                if existing is not None:
                    restored = _reservation_from_json(existing[0])
                    if restored.reservation_id != reservation.reservation_id:
                        raise DurableReservationConflict("DURABLE_EXECUTION_IDENTITY_CONFLICT")
                    connection.commit()
                    return restored, False
                count = connection.execute("SELECT COUNT(*) FROM durable_reservations").fetchone()[0]
                if count >= self._max_records:
                    raise DurableReservationStateError("DURABLE_FIRST_ORDER_RESERVATION_EXISTS")
                connection.execute(
                    """INSERT INTO durable_reservations (
                        reservation_id, execution_identity, status, lifecycle_version,
                        authorization_consumption, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (reservation.reservation_id, reservation.execution_identity, reservation.status.value,
                     reservation.lifecycle_version, reservation.authorization_consumption.value,
                     _reservation_json(reservation)),
                )
                self._insert_lifecycle(connection, reservation, from_status=None, event_state="durable_reservation_created", occurred_at=reservation.created_at)
                self._insert_outbox(connection, reservation, "durable_reservation_created", occurred_at=reservation.created_at)
                self._commit(connection)
                return reservation, True
            except DurableReservationStoreError:
                connection.rollback()
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                self._available = False
                raise DurableReservationUnavailable("durable reservation persistence unavailable") from error
            finally:
                connection.close()

    def project_outbox(self, audit_service: AuditService) -> int:
        """Idempotently project locally committed outbox events into the audit ledger."""
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT outbox_id, event_json FROM durable_reservation_outbox WHERE delivered_at IS NULL ORDER BY outbox_id"
                ).fetchall()
            except sqlite3.DatabaseError as error:
                self._available = False
                raise DurableReservationUnavailable("durable reservation outbox unavailable") from error
            finally:
                connection.close()
        delivered = 0
        for outbox_id, event_json in rows:
            event = _audit_from_json(event_json)
            try:
                audit_service.record(event)
            except AuditStoreError as error:
                raise DurableReservationUnavailable("durable reservation audit projection unavailable") from error
            with self._lock:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    changed = connection.execute(
                        "UPDATE durable_reservation_outbox SET delivered_at = ? WHERE outbox_id = ? AND delivered_at IS NULL",
                        (event.occurred_at.isoformat(), outbox_id),
                    ).rowcount
                    self._commit(connection)
                    delivered += int(changed == 1)
                except (sqlite3.DatabaseError, OSError) as error:
                    connection.rollback()
                    self._available = False
                    raise DurableReservationUnavailable("durable reservation outbox acknowledgement unavailable") from error
                finally:
                    connection.close()
        return delivered

    def promote_ready(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        self._expire_if_due(reservation_id, now=now)
        self._revalidate_or_reconcile(reservation_id, now=now)
        return self._transition(reservation_id=reservation_id, status=DurableReservationStatus.READY_FOR_ATTEMPT, now=now, require_created_audit=True, event_state="durable_reservation_ready")

    def confirm_authorization_consumed_for_test_only(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        """Record only a synthetic proof; no real authorization store is consumed."""
        self._expire_if_due(reservation_id, now=now)
        self._revalidate_or_reconcile(reservation_id, now=now)
        return self._transition(
            reservation_id=reservation_id,
            status=DurableReservationStatus.READY_FOR_ATTEMPT,
            now=now,
            authorization_consumption=DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED,
            allow_same_status=True,
            event_state="durable_authorization_consumption_confirmed_test_only",
        )

    def mark_authorization_ambiguous(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        return self._transition(reservation_id=reservation_id, status=DurableReservationStatus.RECONCILIATION_REQUIRED, now=now, authorization_consumption=DurableAuthorizationConsumptionState.AMBIGUOUS, event_state="durable_authorization_consumption_ambiguous")

    def start_attempt(self, reservation_id: str, *, now: datetime, kill_switch_active: bool) -> DurableExecutionReservation:
        """Commit ATTEMPT_STARTED before any hypothetical future external boundary."""
        if kill_switch_active:
            raise DurableReservationStateError("KILL_SWITCH_ACTIVE")
        self._expire_if_due(reservation_id, now=now)
        self._revalidate_or_reconcile(reservation_id, now=now)
        return self._transition(reservation_id=reservation_id, status=DurableReservationStatus.ATTEMPT_STARTED, now=now, require_consumption=True, require_all_audits=True, event_state="durable_attempt_started")

    def reject_terminal(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        return self._terminal_transition(reservation_id, DurableReservationStatus.REJECTED_TERMINAL, now=now, event_state="durable_attempt_rejected_terminal")

    def complete_terminal(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        """Record only a synthetic terminal outcome; it never claims broker fill."""
        return self._terminal_transition(reservation_id, DurableReservationStatus.COMPLETED_TERMINAL, now=now, event_state="durable_attempt_completed_terminal_test_only")

    def revoke(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        self._expire_if_due(reservation_id, now=now)
        return self._terminal_transition(reservation_id, DurableReservationStatus.REVOKED, now=now, event_state="durable_reservation_revoked")

    def expire(self, reservation_id: str, *, now: datetime) -> DurableExecutionReservation:
        current = self._require_reservation(reservation_id)
        if current.status is DurableReservationStatus.EXPIRED:
            return current
        if current.status not in {DurableReservationStatus.RESERVED, DurableReservationStatus.READY_FOR_ATTEMPT}:
            raise DurableReservationStateError("DURABLE_RESERVATION_LIFECYCLE_INVALID")
        if _require_aware(now) < current.expires_at:
            raise DurableReservationStateError("DURABLE_RESERVATION_NOT_EXPIRED")
        return self._transition(reservation_id=reservation_id, status=DurableReservationStatus.EXPIRED, now=now, event_state="durable_reservation_expired", skip_expiry_guard=True)

    def recover(self, *, now: datetime) -> tuple[DurableExecutionReservation, ...]:
        """Conservatively turn all unfinished state into reconciliation-required."""
        recovered: list[DurableExecutionReservation] = []
        for reservation in self.all():
            if reservation.status in _TERMINAL:
                continue
            if reservation.status in {DurableReservationStatus.RESERVED, DurableReservationStatus.READY_FOR_ATTEMPT} and _require_aware(now) >= reservation.expires_at:
                recovered.append(self.expire(reservation.reservation_id, now=now))
            else:
                recovered.append(self._transition(reservation_id=reservation.reservation_id, status=DurableReservationStatus.RECONCILIATION_REQUIRED, now=now, event_state="durable_restart_reconciliation_required"))
        return tuple(recovered)

    def get(self, reservation_id: str) -> DurableExecutionReservation | None:
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("SELECT payload_json FROM durable_reservations WHERE reservation_id = ?", (reservation_id,)).fetchone()
                return _reservation_from_json(row[0]) if row is not None else None
            except (sqlite3.DatabaseError, ValueError, json.JSONDecodeError) as error:
                self._available = False
                raise DurableReservationUnavailable("durable reservation read unavailable") from error
            finally:
                connection.close()

    def all(self) -> tuple[DurableExecutionReservation, ...]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute("SELECT payload_json FROM durable_reservations ORDER BY execution_identity, reservation_id").fetchall()
                return tuple(_reservation_from_json(row[0]) for row in rows)
            except (sqlite3.DatabaseError, ValueError, json.JSONDecodeError) as error:
                self._available = False
                raise DurableReservationUnavailable("durable reservation read unavailable") from error
            finally:
                connection.close()

    def history(self, reservation_id: str) -> tuple[tuple[int, str | None, str, str], ...]:
        """Read immutable local lifecycle facts in deterministic version order."""
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT lifecycle_version, from_status, to_status, event_state FROM durable_reservation_lifecycle WHERE reservation_id = ? ORDER BY lifecycle_version",
                    (reservation_id,),
                ).fetchall()
                return tuple((int(row[0]), row[1], row[2], row[3]) for row in rows)
            except sqlite3.DatabaseError as error:
                self._available = False
                raise DurableReservationUnavailable("durable reservation lifecycle unavailable") from error
            finally:
                connection.close()

    @property
    def count(self) -> int:
        return len(self.all())

    def _terminal_transition(self, reservation_id: str, status: DurableReservationStatus, *, now: datetime, event_state: str) -> DurableExecutionReservation:
        current = self._require_reservation(reservation_id)
        if current.status is status:
            return current
        return self._transition(reservation_id=reservation_id, status=status, now=now, event_state=event_state)

    def _expire_if_due(self, reservation_id: str, *, now: datetime) -> None:
        current = self._require_reservation(reservation_id)
        if current.status in {DurableReservationStatus.RESERVED, DurableReservationStatus.READY_FOR_ATTEMPT} and _require_aware(now) >= current.expires_at:
            self.expire(reservation_id, now=now)
            raise DurableReservationStateError("DURABLE_RESERVATION_EXPIRED")

    def _require_reservation(self, reservation_id: str) -> DurableExecutionReservation:
        current = self.get(reservation_id)
        if current is None:
            raise DurableReservationStateError("DURABLE_RESERVATION_NOT_FOUND")
        return current

    def _validate_test_only_evaluation(self, reservation: DurableExecutionReservation, *, now: datetime) -> None:
        """Reject malformed, incomplete, stale, or mismatched synthetic output."""
        now_utc = _require_aware(now)
        try:
            result = self._test_only_evaluator.verify(reservation, now=now_utc)
        except Exception as error:
            raise DurableReservationStateError("DURABLE_TEST_EVALUATION_UNAVAILABLE") from error
        if not isinstance(result, DurableTestOnlyReservationEvaluation):
            raise DurableReservationStateError("DURABLE_TEST_EVALUATION_MALFORMED")
        if result.outcome != "test_only_validated" or result.test_only is not True:
            raise DurableReservationStateError("DURABLE_TEST_EVALUATION_REJECTED")
        if result.verified_at != now_utc:
            raise DurableReservationStateError("DURABLE_TEST_EVALUATION_STALE")
        if result.reservation_id != reservation.reservation_id or result.reservation_fingerprint != reservation.fingerprint:
            raise DurableReservationStateError("DURABLE_TEST_EVALUATION_LINEAGE_MISMATCH")
        if result.safety_evidence_fingerprint != reservation.safety_evidence.fingerprint:
            raise DurableReservationStateError("DURABLE_TEST_EVIDENCE_FINGERPRINT_MISMATCH")

    def _revalidate_or_reconcile(self, reservation_id: str, *, now: datetime) -> None:
        """Fail closed and durably require reconciliation when test checks fail.

        The external test evaluator and this SQLite transaction cannot share an
        atomic commit.  Consequently its structural result is rechecked directly
        before each readiness-increasing local transition; any race or failed
        check is conservatively persisted as reconciliation-required.  This is
        not authority verification and cannot enable a submission.
        """
        current = self._require_reservation(reservation_id)
        try:
            self._validate_test_only_evaluation(current, now=now)
        except DurableReservationStateError:
            if current.status in {DurableReservationStatus.RESERVED, DurableReservationStatus.READY_FOR_ATTEMPT}:
                try:
                    self._transition(reservation_id=reservation_id, status=DurableReservationStatus.RECONCILIATION_REQUIRED, now=now, event_state="durable_test_evaluation_failed", skip_expiry_guard=True)
                except DurableReservationStoreError:
                    pass
            raise

    def _transition(
        self,
        *,
        reservation_id: str,
        status: DurableReservationStatus,
        now: datetime,
        authorization_consumption: DurableAuthorizationConsumptionState | None = None,
        require_created_audit: bool = False,
        require_all_audits: bool = False,
        require_consumption: bool = False,
        allow_same_status: bool = False,
        skip_expiry_guard: bool = False,
        event_state: str,
    ) -> DurableExecutionReservation:
        now_utc = _require_aware(now)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT payload_json FROM durable_reservations WHERE reservation_id = ?", (reservation_id,)).fetchone()
                if row is None:
                    raise DurableReservationStateError("DURABLE_RESERVATION_NOT_FOUND")
                current = _reservation_from_json(row[0])
                if current.status is status and allow_same_status and authorization_consumption is DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED:
                    if current.authorization_consumption is DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED:
                        connection.commit()
                        return current
                elif status not in _LEGAL_TRANSITIONS[current.status]:
                    raise DurableReservationStateError("DURABLE_RESERVATION_LIFECYCLE_INVALID")
                if not skip_expiry_guard and now_utc >= current.expires_at and status not in {DurableReservationStatus.RECONCILIATION_REQUIRED, DurableReservationStatus.EXPIRED}:
                    raise DurableReservationStateError("DURABLE_RESERVATION_EXPIRED")
                if require_created_audit:
                    projected = connection.execute("SELECT 1 FROM durable_reservation_outbox WHERE reservation_id = ? AND state = ? AND delivered_at IS NOT NULL", (reservation_id, "durable_reservation_created")).fetchone()
                    if projected is None:
                        raise DurableReservationStateError("DURABLE_RESERVATION_AUDIT_NOT_PROJECTED")
                if require_all_audits:
                    pending = connection.execute("SELECT 1 FROM durable_reservation_outbox WHERE reservation_id = ? AND delivered_at IS NULL", (reservation_id,)).fetchone()
                    if pending is not None:
                        raise DurableReservationStateError("DURABLE_RESERVATION_AUDIT_NOT_PROJECTED")
                if require_consumption and current.authorization_consumption is not DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED:
                    raise DurableReservationStateError("DURABLE_AUTHORIZATION_CONSUMPTION_UNPROVEN")
                updated = current.model_copy(update={
                    "status": status,
                    "authorization_consumption": authorization_consumption or current.authorization_consumption,
                    "lifecycle_version": current.lifecycle_version + 1,
                })
                connection.execute("UPDATE durable_reservations SET status = ?, lifecycle_version = ?, authorization_consumption = ?, payload_json = ? WHERE reservation_id = ?", (updated.status.value, updated.lifecycle_version, updated.authorization_consumption.value, _reservation_json(updated), reservation_id))
                self._insert_lifecycle(connection, updated, from_status=current.status, event_state=event_state, occurred_at=now_utc)
                self._insert_outbox(connection, updated, event_state, occurred_at=now_utc)
                self._commit(connection)
                return updated
            except DurableReservationStoreError:
                connection.rollback()
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                self._available = False
                raise DurableReservationUnavailable("durable reservation transition unavailable") from error
            finally:
                connection.close()

    def _insert_lifecycle(self, connection: sqlite3.Connection, reservation: DurableExecutionReservation, *, from_status: DurableReservationStatus | None, event_state: str, occurred_at: datetime) -> None:
        connection.execute("INSERT INTO durable_reservation_lifecycle (reservation_id, lifecycle_version, from_status, to_status, event_state, occurred_at) VALUES (?, ?, ?, ?, ?, ?)", (reservation.reservation_id, reservation.lifecycle_version, None if from_status is None else from_status.value, reservation.status.value, event_state, occurred_at.isoformat()))

    def _insert_outbox(self, connection: sqlite3.Connection, reservation: DurableExecutionReservation, state: str, *, occurred_at: datetime) -> None:
        event = AuditEvent(
            event_type=AuditEventType.DEMO_SUBMISSION_ATTEMPT,
            occurred_at=occurred_at,
            execution_id=reservation.reservation_id,
            proposal_id=reservation.proposal_id,
            intent_id=reservation.execution_intent_id,
            approval_id=reservation.sentinel_approval_id,
            account_reference=reservation.account_reference,
            state=state,
            reason_code="TEST_ONLY_DURABLE_RESERVATION",
            provenance_fingerprint=reservation.fingerprint,
            details=(("test_only", "true"), ("submission_allowed", "false")),
        )
        outbox_id = f"{reservation.reservation_id}:{state}:{reservation.lifecycle_version}"
        connection.execute("INSERT INTO durable_reservation_outbox (outbox_id, reservation_id, state, event_json, delivered_at) VALUES (?, ?, ?, ?, NULL)", (outbox_id, reservation.reservation_id, state, _audit_json(event)))

    def _commit(self, connection: sqlite3.Connection) -> None:
        if self._fail_next_commit_for_test:
            self._fail_next_commit_for_test = False
            raise sqlite3.OperationalError("synthetic commit failure")
        connection.commit()

    def _initialize(self) -> None:
        try:
            # Separate store instances may be constructed concurrently in one
            # process. Serialize only schema/WAL setup; all reservation state
            # remains protected by SQLite transactions and constraints.
            with self._schema_initialization_lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self._path, timeout=self._timeout_seconds, isolation_level=None)
                try:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
                    required = {"durable_schema_metadata", "durable_reservations", "durable_reservation_outbox", "durable_reservation_lifecycle"}
                    if tables and not required.issubset(tables):
                        raise DurableReservationUnavailable("durable reservation schema is missing or corrupt")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_reservations (reservation_id TEXT PRIMARY KEY, execution_identity TEXT UNIQUE NOT NULL, status TEXT NOT NULL, lifecycle_version INTEGER NOT NULL, authorization_consumption TEXT NOT NULL, payload_json TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_reservation_outbox (outbox_id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL, state TEXT NOT NULL, event_json TEXT NOT NULL, delivered_at TEXT NULL, FOREIGN KEY(reservation_id) REFERENCES durable_reservations(reservation_id))")
                    connection.execute("CREATE TABLE IF NOT EXISTS durable_reservation_lifecycle (reservation_id TEXT NOT NULL, lifecycle_version INTEGER NOT NULL, from_status TEXT NULL, to_status TEXT NOT NULL, event_state TEXT NOT NULL, occurred_at TEXT NOT NULL, PRIMARY KEY(reservation_id, lifecycle_version), FOREIGN KEY(reservation_id) REFERENCES durable_reservations(reservation_id))")
                    version = connection.execute("SELECT value FROM durable_schema_metadata WHERE key = 'schema_version'").fetchone()
                    if version is None:
                        connection.execute("INSERT INTO durable_schema_metadata (key, value) VALUES ('schema_version', ?)", (_SCHEMA_VERSION,))
                    elif version[0] != _SCHEMA_VERSION:
                        raise DurableReservationUnavailable("unsupported durable reservation schema version")
                    connection.commit()
                finally:
                    connection.close()
        except DurableReservationStoreError:
            self._available = False
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            self._available = False
            raise DurableReservationUnavailable("durable reservation database unavailable") from error

    def _connect(self) -> sqlite3.Connection:
        if not self._available:
            raise DurableReservationUnavailable("durable reservation database unavailable")
        return sqlite3.connect(self._path, timeout=self._timeout_seconds, isolation_level=None)


def _reservation_json(value: DurableExecutionReservation) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _reservation_from_json(value: str) -> DurableExecutionReservation:
    data = json.loads(value)
    for computed in ("execution_identity", "reservation_id", "fingerprint"):
        data.pop(computed, None)
    if isinstance(data.get("safety_evidence"), dict):
        data["safety_evidence"].pop("fingerprint", None)
    return DurableExecutionReservation.model_validate(data)


def _audit_json(value: AuditEvent) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _audit_from_json(value: str) -> AuditEvent:
    data = json.loads(value)
    for computed in ("fingerprint", "event_id"):
        data.pop(computed, None)
    return AuditEvent.model_validate(data)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("durable reservation timestamps require timezone awareness")
    return value.astimezone(UTC)
