"""Provider-neutral reconciliation. It flags discrepancies and never retries."""

from __future__ import annotations

from datetime import datetime

from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import (
    BrokerOrderObservation,
    ExecutionLifecycleState,
    ExecutionRecord,
    ReconciliationReport,
    ReconciliationStatus,
)


def reconcile(
    *,
    store: ExecutionStateStore,
    record: ExecutionRecord,
    broker: BrokerOrderObservation | None,
    now: datetime,
) -> ReconciliationReport:
    """Compare local intent-derived facts with sanitized broker observations."""
    if broker is None:
        updated = _require_reconciliation(store, record, now, "MISSING_BROKER_ORDER")
        return ReconciliationReport(execution_id=updated.execution_id, status=ReconciliationStatus.MISSING_BROKER_ORDER, reason_codes=("MISSING_BROKER_ORDER",), record_fingerprint=updated.fingerprint)
    mismatches = _mismatches(record, broker)
    if mismatches:
        updated = _require_reconciliation(store, record, now, "PARAMETER_MISMATCH")
        return ReconciliationReport(execution_id=updated.execution_id, status=ReconciliationStatus.PARAMETER_MISMATCH, reason_codes=mismatches, record_fingerprint=updated.fingerprint)
    status = broker.status.strip().upper()
    target, reconciliation = {
        "FILLED": (ExecutionLifecycleState.FILLED, ReconciliationStatus.FILLED),
        "PARTIALLY_FILLED": (ExecutionLifecycleState.PARTIALLY_FILLED, ReconciliationStatus.PARTIAL_FILL),
        "REJECTED": (ExecutionLifecycleState.REJECTED, ReconciliationStatus.REJECTED),
        "CANCELLED": (ExecutionLifecycleState.CANCELLED, ReconciliationStatus.CANCELLED),
    }.get(status, (ExecutionLifecycleState.RECONCILIATION_REQUIRED, ReconciliationStatus.UNKNOWN))
    updated = _transition_if_needed(store, record, target, now, broker, reconciliation)
    reasons = () if reconciliation is not ReconciliationStatus.UNKNOWN else ("UNKNOWN_BROKER_STATE", "RECONCILIATION_REQUIRED")
    return ReconciliationReport(execution_id=updated.execution_id, status=reconciliation, reason_codes=reasons, record_fingerprint=updated.fingerprint)


def _mismatches(record: ExecutionRecord, broker: BrokerOrderObservation) -> tuple[str, ...]:
    codes: list[str] = []
    if broker.account_id is not None and broker.account_id != record.account_id: codes.append("ACCOUNT_MISMATCH")
    if broker.canonical_symbol is not None and broker.canonical_symbol != record.canonical_symbol: codes.append("SYMBOL_MISMATCH")
    if record.broker_symbol and broker.broker_symbol and record.broker_symbol != broker.broker_symbol: codes.append("BROKER_SYMBOL_UNRESOLVED")
    if broker.direction is not None and broker.direction.lower() != record.direction.lower(): codes.append("DIRECTION_MISMATCH")
    if record.quantity is not None and broker.requested_quantity is not None and broker.requested_quantity != record.quantity: codes.append("QUANTITY_MISMATCH")
    if broker.requested_price is not None and broker.requested_price != record.requested_price: codes.append("PRICE_MISMATCH")
    if broker.stop_loss is not None and broker.stop_loss != record.stop_loss: codes.append("STOP_MISMATCH")
    if broker.take_profit is not None and broker.take_profit != record.take_profit: codes.append("TARGET_MISMATCH")
    return tuple(sorted(codes))


def _require_reconciliation(store: ExecutionStateStore, record: ExecutionRecord, now: datetime, reason: str) -> ExecutionRecord:
    if record.state is ExecutionLifecycleState.RECONCILIATION_REQUIRED:
        return record
    return store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.RECONCILIATION_REQUIRED, now=now, reason_code=reason, reconciliation_status=ReconciliationStatus.RECONCILIATION_REQUIRED)


def _transition_if_needed(store: ExecutionStateStore, record: ExecutionRecord, target: ExecutionLifecycleState, now: datetime, broker: BrokerOrderObservation, status: ReconciliationStatus) -> ExecutionRecord:
    if record.state is target:
        return record
    return store.transition(execution_id=record.execution_id, to_state=target, now=now, broker_order_id=broker.broker_order_id, broker_position_id=broker.broker_position_id, broker_symbol=broker.broker_symbol, broker_status=broker.status, reconciliation_status=status)
