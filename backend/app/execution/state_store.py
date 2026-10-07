"""Bounded, thread-safe provider-neutral execution lifecycle store."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from threading import RLock
from typing import Iterator

from app.models.execution_state import (
    ExecutionLifecycleState,
    ExecutionRecord,
    ExecutionTransition,
    ReconciliationStatus,
)


_ALLOWED: dict[ExecutionLifecycleState, frozenset[ExecutionLifecycleState]] = {
    ExecutionLifecycleState.INTENT_CREATED: frozenset({ExecutionLifecycleState.VALIDATED, ExecutionLifecycleState.BLOCKED, ExecutionLifecycleState.EXPIRED, ExecutionLifecycleState.RECONCILIATION_REQUIRED, ExecutionLifecycleState.KILL_SWITCHED}),
    ExecutionLifecycleState.VALIDATED: frozenset({ExecutionLifecycleState.READY_FOR_SUBMISSION, ExecutionLifecycleState.BLOCKED, ExecutionLifecycleState.EXPIRED, ExecutionLifecycleState.KILL_SWITCHED}),
    ExecutionLifecycleState.READY_FOR_SUBMISSION: frozenset({ExecutionLifecycleState.DRY_RUN_VALIDATED, ExecutionLifecycleState.SUBMISSION_IN_PROGRESS, ExecutionLifecycleState.BLOCKED, ExecutionLifecycleState.EXPIRED, ExecutionLifecycleState.KILL_SWITCHED, ExecutionLifecycleState.RECONCILIATION_REQUIRED}),
    ExecutionLifecycleState.SUBMISSION_IN_PROGRESS: frozenset({ExecutionLifecycleState.SUBMITTED, ExecutionLifecycleState.REJECTED, ExecutionLifecycleState.UNKNOWN, ExecutionLifecycleState.FAILED}),
    ExecutionLifecycleState.SUBMITTED: frozenset({ExecutionLifecycleState.ACKNOWLEDGED, ExecutionLifecycleState.PARTIALLY_FILLED, ExecutionLifecycleState.FILLED, ExecutionLifecycleState.REJECTED, ExecutionLifecycleState.UNKNOWN, ExecutionLifecycleState.RECONCILIATION_REQUIRED}),
    ExecutionLifecycleState.ACKNOWLEDGED: frozenset({ExecutionLifecycleState.PARTIALLY_FILLED, ExecutionLifecycleState.FILLED, ExecutionLifecycleState.REJECTED, ExecutionLifecycleState.CANCEL_REQUESTED, ExecutionLifecycleState.UNKNOWN, ExecutionLifecycleState.RECONCILIATION_REQUIRED}),
    ExecutionLifecycleState.PARTIALLY_FILLED: frozenset({ExecutionLifecycleState.FILLED, ExecutionLifecycleState.CANCEL_REQUESTED, ExecutionLifecycleState.UNKNOWN, ExecutionLifecycleState.RECONCILIATION_REQUIRED}),
    ExecutionLifecycleState.CANCEL_REQUESTED: frozenset({ExecutionLifecycleState.CANCELLED, ExecutionLifecycleState.UNKNOWN, ExecutionLifecycleState.RECONCILIATION_REQUIRED}),
    ExecutionLifecycleState.UNKNOWN: frozenset({ExecutionLifecycleState.RECONCILIATION_REQUIRED}),
    ExecutionLifecycleState.RECONCILIATION_REQUIRED: frozenset({ExecutionLifecycleState.ACKNOWLEDGED, ExecutionLifecycleState.PARTIALLY_FILLED, ExecutionLifecycleState.FILLED, ExecutionLifecycleState.REJECTED, ExecutionLifecycleState.CANCELLED, ExecutionLifecycleState.UNKNOWN}),
    ExecutionLifecycleState.BLOCKED: frozenset(),
    ExecutionLifecycleState.DRY_RUN_VALIDATED: frozenset({ExecutionLifecycleState.BLOCKED}),
    ExecutionLifecycleState.KILL_SWITCHED: frozenset(),
    ExecutionLifecycleState.FILLED: frozenset(),
    ExecutionLifecycleState.REJECTED: frozenset(),
    ExecutionLifecycleState.CANCELLED: frozenset(),
    ExecutionLifecycleState.EXPIRED: frozenset(),
    ExecutionLifecycleState.FAILED: frozenset(),
}
class ExecutionStateStore:
    """Process-local records. Restart intentionally cannot prove prior broker state."""

    def __init__(self, *, max_records: int = 1_000) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self._max_records = max_records
        self._records: dict[str, ExecutionRecord] = {}
        self._attempts: dict[str, str] = {}
        self._transitions: dict[str, tuple[ExecutionTransition, ...]] = {}
        self._submission_locks: dict[str, RLock] = {}
        self._lock = RLock()

    def create_or_get(self, record: ExecutionRecord) -> tuple[ExecutionRecord, bool]:
        """Return the existing exact attempt or reject a conflicting attempt."""
        with self._lock:
            existing_id = self._attempts.get(record.attempt_identity)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.idempotency_key != record.idempotency_key:
                    raise ValueError("CONFLICTING_EXECUTION")
                return existing, False
            if len(self._records) >= self._max_records:
                raise RuntimeError("EXECUTION_STORE_CAPACITY")
            self._records[record.execution_id] = record
            self._attempts[record.attempt_identity] = record.execution_id
            self._transitions[record.execution_id] = ()
            self._submission_locks[record.execution_id] = RLock()
            return record, True

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with self._lock:
            return self._records.get(execution_id)

    def transitions(self, execution_id: str) -> tuple[ExecutionTransition, ...]:
        with self._lock:
            return self._transitions.get(execution_id, ())

    def transition(
        self,
        *,
        execution_id: str,
        to_state: ExecutionLifecycleState,
        now: datetime,
        reason_code: str | None = None,
        broker_symbol: str | None = None,
        broker_order_id: str | None = None,
        broker_position_id: str | None = None,
        broker_status: str | None = None,
        reconciliation_status: ReconciliationStatus | None = None,
    ) -> ExecutionRecord:
        with self._lock:
            current = self._records.get(execution_id)
            if current is None:
                raise KeyError("UNKNOWN_EXECUTION")
            if to_state not in _ALLOWED[current.state]:
                raise ValueError("INVALID_EXECUTION_TRANSITION")
            updated = current.model_copy(update={
                "state": to_state, "updated_at": now, "lifecycle_version": current.lifecycle_version + 1,
                "failure_reason": reason_code, "broker_symbol": broker_symbol or current.broker_symbol,
                "broker_order_id": broker_order_id or current.broker_order_id,
                "broker_position_id": broker_position_id or current.broker_position_id,
                "last_broker_status": broker_status or current.last_broker_status,
                "reconciliation_status": reconciliation_status or current.reconciliation_status,
                "submitted_at": now if to_state is ExecutionLifecycleState.SUBMITTED else current.submitted_at,
            })
            self._records[execution_id] = updated
            transition = ExecutionTransition(execution_id=execution_id, from_state=current.state, to_state=to_state, reason_code=reason_code, occurred_at=now, lifecycle_version=updated.lifecycle_version)
            self._transitions[execution_id] = (*self._transitions[execution_id], transition)
            return updated

    @contextmanager
    def submission_guard(self, execution_id: str) -> Iterator[bool]:
        """Serialize a submission attempt without holding the global store lock."""
        with self._lock:
            lock = self._submission_locks.get(execution_id)
        if lock is None:
            yield False
            return
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    def mark_restart_recovery_required(self, record: ExecutionRecord, *, now: datetime) -> ExecutionRecord:
        """A lost process-local state cannot prove a broker order was absent."""
        with self._lock:
            existing = self._records.get(record.execution_id)
            if existing is None:
                recovery = record.model_copy(update={
                    "state": ExecutionLifecycleState.RECONCILIATION_REQUIRED,
                    "updated_at": now,
                    "reconciliation_status": ReconciliationStatus.RECONCILIATION_REQUIRED,
                    "failure_reason": "STALE_EXECUTION_STATE",
                    "lifecycle_version": record.lifecycle_version + 1,
                })
                if len(self._records) >= self._max_records:
                    raise RuntimeError("EXECUTION_STORE_CAPACITY")
                self._records[recovery.execution_id] = recovery
                self._attempts[recovery.attempt_identity] = recovery.execution_id
                self._transitions[recovery.execution_id] = ()
                self._submission_locks[recovery.execution_id] = RLock()
                return recovery
            return existing

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)
