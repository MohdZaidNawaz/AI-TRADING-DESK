from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest

from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import build_execution_record
from tests.test_execution import _intent


NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _record(**changes):
    return build_execution_record(_intent(account_id="account-1", **changes), execution_mode="disabled", now=NOW)


def test_same_intent_is_idempotent_but_conflicting_attempt_fails() -> None:
    store = ExecutionStateStore(); first, created = store.create_or_get(_record())
    same, created_again = store.create_or_get(_record())
    assert created and not created_again and same.execution_id == first.execution_id
    with pytest.raises(ValueError, match="CONFLICTING_EXECUTION"):
        store.create_or_get(_record(requested_entry=101.0))


def test_submission_guard_serializes_concurrent_attempts() -> None:
    store = ExecutionStateStore(); record, _ = store.create_or_get(_record())
    entered = Event(); release = Event()
    def hold():
        with store.submission_guard(record.execution_id) as locked:
            if locked:
                entered.set(); release.wait(timeout=1)
            return locked
    def contend():
        entered.wait(timeout=1)
        with store.submission_guard(record.execution_id) as locked:
            return locked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(hold)
        second = pool.submit(contend)
        assert second.result(timeout=1) is False
        release.set()
        assert first.result(timeout=1) is True


def test_store_is_bounded_and_retains_active_safety_state() -> None:
    store = ExecutionStateStore(max_records=1)
    first, _ = store.create_or_get(_record())
    with pytest.raises(RuntimeError, match="EXECUTION_STORE_CAPACITY"):
        store.create_or_get(_record(proposal_id="other"))
    assert store.get(first.execution_id) is not None
