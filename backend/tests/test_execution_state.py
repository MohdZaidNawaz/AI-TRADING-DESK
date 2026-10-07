from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import ExecutionLifecycleState, build_execution_record
from tests.test_execution import _intent


NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _record(**changes):
    return build_execution_record(_intent(account_id="account-1", **changes), execution_mode="disabled", now=NOW)


def test_execution_record_is_immutable_and_identity_is_deterministic() -> None:
    first = _record()
    assert first.execution_id == _record().execution_id
    assert first.idempotency_key == _record().idempotency_key
    with pytest.raises(ValidationError):
        first.state = ExecutionLifecycleState.FILLED


def test_valid_lifecycle_transitions_are_controlled() -> None:
    store = ExecutionStateStore()
    record, created = store.create_or_get(_record())
    assert created
    record = store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.VALIDATED, now=NOW)
    record = store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.READY_FOR_SUBMISSION, now=NOW)
    record = store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.SUBMISSION_IN_PROGRESS, now=NOW)
    record = store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.SUBMITTED, now=NOW)
    assert record.state is ExecutionLifecycleState.SUBMITTED
    assert tuple(item.to_state for item in store.transitions(record.execution_id))[-1] is ExecutionLifecycleState.SUBMITTED


def test_invalid_and_terminal_transitions_fail_closed() -> None:
    store = ExecutionStateStore(); record, _ = store.create_or_get(_record())
    with pytest.raises(ValueError, match="INVALID_EXECUTION_TRANSITION"):
        store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.FILLED, now=NOW)
    record = store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.EXPIRED, now=NOW)
    with pytest.raises(ValueError, match="INVALID_EXECUTION_TRANSITION"):
        store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.VALIDATED, now=NOW)
