import pytest

from app.execution.intent_store import ExecutionIntentStore
from app.models.execution import ExecutionStatus
from tests.test_execution import _intent


def test_identical_requests_are_idempotent() -> None:
    store = ExecutionIntentStore()
    first = store.put(_intent())
    assert store.put(_intent()) is first
    assert store.count == 1


def test_conflicting_duplicate_identity_fails_closed() -> None:
    store = ExecutionIntentStore()
    store.put(_intent())
    with pytest.raises(ValueError, match="conflicting_execution_intent"):
        store.put(_intent(status=ExecutionStatus.CREATED))


def test_store_is_bounded_with_deterministic_eviction() -> None:
    store = ExecutionIntentStore(max_records=1)
    store.put(_intent(proposal_id="later", expires_at=_intent().expires_at))
    store.put(_intent(proposal_id="earlier", expires_at=_intent().created_at))
    assert store.count == 1
    assert store.put(_intent(proposal_id="later", expires_at=_intent().expires_at)).proposal_id == "later"


def test_store_rejects_an_invalid_bound() -> None:
    with pytest.raises(ValueError, match="positive"):
        ExecutionIntentStore(max_records=0)
