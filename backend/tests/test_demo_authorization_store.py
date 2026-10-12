"""Focused bounded-store tests for Phase 11 demo pre-authorizations."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.execution.demo_authorization_store import DemoAuthorizationStore, DemoAuthorizationStoreError
from app.models.demo_authorization import DemoAuthorizationStatus
from tests.test_demo_execution_authorization import _authorize
from tests.test_demo_execution_readiness import NOW


def test_store_is_bounded_thread_safe_style_and_does_not_evict(tmp_path) -> None:
    _service, store, _registry, _audit, _state, _values, result = _authorize(tmp_path)
    authorization = result.authorization
    assert authorization is not None and store.snapshot() == (authorization,)
    with pytest.raises(DemoAuthorizationStoreError, match="DUPLICATE"):
        store.create(authorization)
    alternate = authorization.model_copy(update={"proposal_id": "different"})
    with pytest.raises(DemoAuthorizationStoreError, match="SINGLE_AUTHORIZATION_LIMIT"):
        store.create(alternate)
    assert store.count == 1


def test_store_expiry_reconciliation_and_immutable_status_transitions(tmp_path) -> None:
    _service, store, _registry, _audit, _state, _values, result = _authorize(tmp_path)
    authorization = result.authorization
    assert authorization is not None
    with pytest.raises(DemoAuthorizationStoreError, match="EXPIRED"):
        store.verify(authorization_id=authorization.authorization_id, now=authorization.expires_at)
    expired = store.get(authorization.authorization_id)
    assert expired is not None and expired.status is DemoAuthorizationStatus.EXPIRED
    # A new safe store demonstrates the explicit reconciliation state without
    # relying on any broker process or mutable nested record.
    fresh = DemoAuthorizationStore(); fresh.confirm_clean_start(); fresh.create(authorization)
    required = fresh.mark_reconciliation_required(authorization_id=authorization.authorization_id, now=NOW)
    assert required.status is DemoAuthorizationStatus.RECONCILIATION_REQUIRED
    with pytest.raises(DemoAuthorizationStoreError, match="RECONCILIATION_REQUIRED"):
        fresh.verify(authorization_id=authorization.authorization_id, now=NOW)
    fresh.mark_recovery_uncertain(now=NOW + timedelta(seconds=1))
    with pytest.raises(DemoAuthorizationStoreError, match="RECOVERY_REQUIRED"):
        fresh.verify(authorization_id=authorization.authorization_id, now=NOW + timedelta(seconds=1))
