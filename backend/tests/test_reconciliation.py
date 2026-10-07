from datetime import UTC, datetime

from app.execution.reconciliation import reconcile
from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import BrokerOrderObservation, ExecutionLifecycleState, build_execution_record
from tests.test_execution import _intent


NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _submitted():
    store = ExecutionStateStore(); record, _ = store.create_or_get(build_execution_record(_intent(account_id="account-1", quantity=1.0), execution_mode="live", now=NOW))
    for state in (ExecutionLifecycleState.VALIDATED, ExecutionLifecycleState.READY_FOR_SUBMISSION, ExecutionLifecycleState.SUBMISSION_IN_PROGRESS, ExecutionLifecycleState.SUBMITTED):
        record = store.transition(execution_id=record.execution_id, to_state=state, now=NOW)
    return store, record


def _broker(status="FILLED", **changes):
    values = dict(broker_order_id="order-1", canonical_symbol="XAUUSD", broker_symbol=None, direction="long", requested_quantity=1.0, requested_price=100.0, stop_loss=95.0, take_profit=110.0, status=status, account_id="account-1")
    values.update(changes)
    return BrokerOrderObservation(**values)


def test_reconciliation_matches_fill_without_retry() -> None:
    store, record = _submitted()
    report = reconcile(store=store, record=record, broker=_broker(), now=NOW)
    assert report.status.value == "filled"
    assert store.get(record.execution_id).state.value == "filled"


def test_missing_unknown_and_mismatched_broker_state_require_reconciliation() -> None:
    store, record = _submitted()
    missing = reconcile(store=store, record=record, broker=None, now=NOW)
    assert missing.status.value == "missing_broker_order"
    store, record = _submitted()
    mismatch = reconcile(store=store, record=record, broker=_broker(requested_quantity=2.0), now=NOW)
    assert mismatch.status.value == "parameter_mismatch"
    store, record = _submitted()
    unknown = reconcile(store=store, record=record, broker=_broker(status="PENDING_UNKNOWN"), now=NOW)
    assert unknown.status.value == "unknown"
