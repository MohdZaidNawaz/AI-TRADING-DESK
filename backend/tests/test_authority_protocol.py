"""Phase 13B authority protocol and compatibility-façade tests."""

from __future__ import annotations

from datetime import timedelta
import inspect

import pytest
from pydantic import ValidationError

from app.execution.authority_protocol import DurableAuthorityProtocolAdapter
from app.execution.demo_authorization_store import (
    DemoAuthorizationStore,
    DemoAuthorizationStoreError,
)
from app.execution.durable_authority_store import DurableAuthorityStore
from app.models.authority_protocol import (
    ApprovalAuthorityRequest,
    AuthorityBackendScope,
    AuthorityOperation,
    AuthorityOperationOutcome,
    AuthorityOperationResult,
    AuthorityRecordKind,
    AuthorityTransitionRequest,
    DemoAuthorizationAuthorityRequest,
)
from app.models.durable_authority import DurableAuthorityStatus
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry
from tests.test_demo_execution_authorization import _authorize
from tests.test_demo_execution_readiness import NOW


def _records(tmp_path):
    _service, _memory_store, _registry, _audit, _state, values, result = _authorize(tmp_path / "inputs")
    assert result.authorization is not None
    return values["decision"], result.authorization


def _adapter(tmp_path) -> tuple[DurableAuthorityStore, DurableAuthorityProtocolAdapter]:
    store = DurableAuthorityStore(tmp_path / "authority.sqlite3")
    return store, DurableAuthorityProtocolAdapter(store)


def _result(**updates: object) -> AuthorityOperationResult:
    values: dict[str, object] = {
        "record_kind": AuthorityRecordKind.APPROVAL,
        "operation": AuthorityOperation.REGISTER,
        "outcome": AuthorityOperationOutcome.APPLIED,
        "backend_scope": AuthorityBackendScope.DURABLE_LIFECYCLE_ONLY,
        "external_id": "approval-1",
        "status": DurableAuthorityStatus.ACTIVE,
        "lifecycle_version": 1,
        "source_fingerprint": "a" * 64,
        "expires_at": NOW + timedelta(minutes=1),
        "reason_code": None,
    }
    values.update(updates)
    return AuthorityOperationResult(**values)


def test_protocol_requests_are_typed_frozen_and_reject_invalid_operations(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    request = ApprovalAuthorityRequest(
        operation=AuthorityOperation.REGISTER,
        decision=decision,
        requested_at=NOW,
    )
    assert request.requested_at == NOW
    with pytest.raises(ValidationError):
        request.operation = AuthorityOperation.CONSUME  # type: ignore[misc]
    with pytest.raises(ValidationError, match="approval requests support"):
        ApprovalAuthorityRequest(
            operation=AuthorityOperation.REVOKE,
            decision=decision,
            requested_at=NOW,
        )
    with pytest.raises(ValidationError, match="demo authorization requests support"):
        DemoAuthorizationAuthorityRequest(
            operation=AuthorityOperation.RECONCILIATION_REQUIRED,
            authorization=authorization,
            requested_at=NOW,
        )


def test_authority_result_rejects_forged_or_ambiguous_successes_at_construction() -> None:
    # This is the exact ambiguity identified by the independent audit: a
    # caller must not be able to manufacture a production-authoritative result
    # with no lineage merely by constructing the public result model.
    with pytest.raises(ValidationError, match="production-authoritative"):
        _result(
            backend_scope=AuthorityBackendScope.PRODUCTION_AUTHORITATIVE,
            status=None,
            lifecycle_version=None,
            source_fingerprint=None,
            expires_at=None,
        )

    for field in ("status", "lifecycle_version", "source_fingerprint", "expires_at"):
        with pytest.raises(ValidationError, match="complete lifecycle lineage and expiry"):
            _result(**{field: None})

    for updates in (
        {"external_id": 1},
        {"external_id": "   "},
        {"external_id": " approval-1"},
        {"lifecycle_version": "1"},
        {"lifecycle_version": True},
        {"status": 1},
        {"source_fingerprint": "A" * 64},
        {"source_fingerprint": "g" * 64},
        {"source_fingerprint": b"a" * 64},
        {"status": "not-a-lifecycle-status"},
        {"expires_at": "not-a-timestamp"},
        {"reason_code": "UNEXPECTED_REASON"},
    ):
        with pytest.raises(ValidationError):
            _result(**updates)


def test_authority_result_requires_operation_consistent_success_lifecycle() -> None:
    valid = (
        {},
        {
            "operation": AuthorityOperation.REGISTER,
            "outcome": AuthorityOperationOutcome.ALREADY_APPLIED,
            "status": DurableAuthorityStatus.REVOKED,
        },
        {"operation": AuthorityOperation.VERIFY, "status": DurableAuthorityStatus.ACTIVE},
        {"operation": AuthorityOperation.CONSUME, "status": DurableAuthorityStatus.CONSUMED},
        {"operation": AuthorityOperation.REVOKE, "status": DurableAuthorityStatus.REVOKED},
        {
            "operation": AuthorityOperation.RECONCILIATION_REQUIRED,
            "status": DurableAuthorityStatus.RECONCILIATION_REQUIRED,
        },
    )
    for updates in valid:
        result = _result(**updates)
        assert result.backend_scope is AuthorityBackendScope.DURABLE_LIFECYCLE_ONLY
        assert result.status is not None
        assert result.lifecycle_version == 1
        assert result.source_fingerprint == "a" * 64
        assert result.expires_at == NOW + timedelta(minutes=1)

    invalid = (
        {"operation": AuthorityOperation.REGISTER, "status": DurableAuthorityStatus.CONSUMED},
        {
            "operation": AuthorityOperation.VERIFY,
            "outcome": AuthorityOperationOutcome.ALREADY_APPLIED,
        },
        {"operation": AuthorityOperation.VERIFY, "status": DurableAuthorityStatus.CONSUMED},
        {"operation": AuthorityOperation.CONSUME, "status": DurableAuthorityStatus.ACTIVE},
        {"operation": AuthorityOperation.REVOKE, "status": DurableAuthorityStatus.ACTIVE},
        {
            "operation": AuthorityOperation.RECONCILIATION_REQUIRED,
            "status": DurableAuthorityStatus.REVOKED,
        },
    )
    for updates in invalid:
        with pytest.raises(ValidationError):
            _result(**updates)


def test_authority_result_rejections_require_deterministic_noncontradictory_state() -> None:
    rejected = _result(
        outcome=AuthorityOperationOutcome.REJECTED,
        status=None,
        lifecycle_version=None,
        source_fingerprint=None,
        expires_at=None,
        reason_code="AUTHORITY_NOT_FOUND",
    )
    assert rejected.reason_code == "AUTHORITY_NOT_FOUND"

    for updates in (
        {"reason_code": None},
        {"reason_code": "   "},
        {"status": None},
        {"source_fingerprint": None},
        {"expires_at": None},
    ):
        values = {
            "outcome": AuthorityOperationOutcome.REJECTED,
            "reason_code": "AUTHORITY_REJECTED",
        }
        values.update(updates)
        with pytest.raises(ValidationError):
            _result(**values)


def test_durable_protocol_has_deterministic_non_consuming_lifecycle_results(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store, adapter = _adapter(tmp_path)
    register = ApprovalAuthorityRequest(
        operation=AuthorityOperation.REGISTER, decision=decision, requested_at=NOW
    )
    registered = adapter.register_approval(register)
    assert registered.outcome is AuthorityOperationOutcome.APPLIED
    assert registered.backend_scope is AuthorityBackendScope.DURABLE_LIFECYCLE_ONLY
    assert registered.status == DurableAuthorityStatus.ACTIVE.value
    assert adapter.register_approval(register).outcome is AuthorityOperationOutcome.ALREADY_APPLIED

    verified = adapter.verify_approval(
        ApprovalAuthorityRequest(operation=AuthorityOperation.VERIFY, decision=decision, requested_at=NOW)
    )
    assert verified.outcome is AuthorityOperationOutcome.APPLIED
    assert store.get_approval(decision.approval_id or "").status is DurableAuthorityStatus.ACTIVE

    consumed = adapter.consume_approval(
        ApprovalAuthorityRequest(operation=AuthorityOperation.CONSUME, decision=decision, requested_at=NOW)
    )
    assert consumed.outcome is AuthorityOperationOutcome.APPLIED
    assert consumed.status == DurableAuthorityStatus.CONSUMED.value
    assert adapter.consume_approval(
        ApprovalAuthorityRequest(operation=AuthorityOperation.CONSUME, decision=decision, requested_at=NOW)
    ).outcome is AuthorityOperationOutcome.ALREADY_APPLIED

    issued = adapter.register_demo_authorization(
        DemoAuthorizationAuthorityRequest(
            operation=AuthorityOperation.REGISTER, authorization=authorization, requested_at=NOW
        )
    )
    assert issued.outcome is AuthorityOperationOutcome.APPLIED
    reconciled = adapter.transition(
        AuthorityTransitionRequest(
            record_kind=AuthorityRecordKind.DEMO_AUTHORIZATION,
            operation=AuthorityOperation.RECONCILIATION_REQUIRED,
            external_id=authorization.authorization_id,
            requested_at=NOW + timedelta(seconds=1),
        )
    )
    assert reconciled.outcome is AuthorityOperationOutcome.APPLIED
    assert reconciled.status == DurableAuthorityStatus.RECONCILIATION_REQUIRED.value
    assert adapter.verify_demo_authorization(
        DemoAuthorizationAuthorityRequest(
            operation=AuthorityOperation.VERIFY,
            authorization=authorization,
            requested_at=NOW + timedelta(seconds=2),
        )
    ).outcome is AuthorityOperationOutcome.REJECTED


def test_protocol_expiry_and_invalid_transition_return_fail_closed_results(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store, adapter = _adapter(tmp_path)
    adapter.register_approval(
        ApprovalAuthorityRequest(operation=AuthorityOperation.REGISTER, decision=decision, requested_at=NOW)
    )
    expired = adapter.verify_approval(
        ApprovalAuthorityRequest(
            operation=AuthorityOperation.VERIFY,
            decision=decision,
            requested_at=decision.expires_at,
        )
    )
    assert expired.outcome is AuthorityOperationOutcome.REJECTED
    assert expired.status == DurableAuthorityStatus.EXPIRED.value
    assert expired.reason_code == "DURABLE_AUTHORITY_EXPIRED"

    adapter.register_demo_authorization(
        DemoAuthorizationAuthorityRequest(
            operation=AuthorityOperation.REGISTER, authorization=authorization, requested_at=NOW
        )
    )
    first = adapter.transition(
        AuthorityTransitionRequest(
            record_kind=AuthorityRecordKind.DEMO_AUTHORIZATION,
            operation=AuthorityOperation.REVOKE,
            external_id=authorization.authorization_id,
            requested_at=NOW,
        )
    )
    assert first.outcome is AuthorityOperationOutcome.APPLIED
    repeated = adapter.transition(
        AuthorityTransitionRequest(
            record_kind=AuthorityRecordKind.DEMO_AUTHORIZATION,
            operation=AuthorityOperation.REVOKE,
            external_id=authorization.authorization_id,
            requested_at=NOW,
        )
    )
    assert repeated.outcome is AuthorityOperationOutcome.ALREADY_APPLIED
    invalid = adapter.transition(
        AuthorityTransitionRequest(
            record_kind=AuthorityRecordKind.DEMO_AUTHORIZATION,
            operation=AuthorityOperation.RECONCILIATION_REQUIRED,
            external_id=authorization.authorization_id,
            requested_at=NOW,
        )
    )
    assert invalid.outcome is AuthorityOperationOutcome.REJECTED
    assert invalid.reason_code == "DURABLE_AUTHORITY_LIFECYCLE_INVALID"
    assert store.get_demo_authorization(authorization.authorization_id).status is DurableAuthorityStatus.REVOKED


def test_legacy_facades_keep_candidate_protocol_disconnected_and_fail_closed(tmp_path) -> None:
    _decision, authorization = _records(tmp_path)
    durable_store, candidate = _adapter(tmp_path)
    registry = ApprovalRegistry(authority_protocol=candidate)
    demo_store = DemoAuthorizationStore(authority_protocol=candidate)
    assert registry.authority_integration_state == "CANDIDATE_PROTOCOL_DISCONNECTED"
    assert demo_store.authority_integration_state == "CANDIDATE_PROTOCOL_DISCONNECTED"
    demo_store.confirm_clean_start()
    assert demo_store.create(authorization) == authorization
    assert demo_store.verify(authorization_id=authorization.authorization_id, now=NOW) == authorization
    assert durable_store.get_demo_authorization(authorization.authorization_id) is None

    with pytest.raises(ApprovalClaimError, match="AUTHORITATIVE_DURABLE_APPROVAL_BACKEND_UNAVAILABLE"):
        ApprovalRegistry(authority_protocol=candidate, require_authoritative_protocol=True)
    with pytest.raises(ApprovalClaimError, match="AUTHORITATIVE_DURABLE_APPROVAL_BACKEND_UNAVAILABLE"):
        ApprovalRegistry(require_authoritative_protocol=True)
    with pytest.raises(
        DemoAuthorizationStoreError,
        match="AUTHORITATIVE_DURABLE_DEMO_AUTHORIZATION_BACKEND_UNAVAILABLE",
    ):
        DemoAuthorizationStore(authority_protocol=candidate, require_authoritative_protocol=True)
    with pytest.raises(
        DemoAuthorizationStoreError,
        match="AUTHORITATIVE_DURABLE_DEMO_AUTHORIZATION_BACKEND_UNAVAILABLE",
    ):
        DemoAuthorizationStore(require_authoritative_protocol=True)


def test_protocol_candidate_cannot_enable_execution_or_reach_submission() -> None:
    import app.execution.authority_protocol as protocol

    source = inspect.getsource(protocol)
    for forbidden in (
        "order_send",
        "MT5ExecutionAdapter",
        ".submit(",
        "from app.execution.pilot",
        "from app.services.execution_gate",
    ):
        assert forbidden not in source
