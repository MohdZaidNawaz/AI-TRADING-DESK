"""Formal Sentinel policy-governance tests using synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalArtifact,
    SentinelPolicyApprovalMechanism,
    SentinelPolicyApprovalScope,
    SentinelPolicyApprovalSourceResult,
    SentinelPolicyApprovalSourceStatus,
    SentinelPolicyApprovalState,
)
from app.models.sentinel_policy_governance import (
    SentinelPolicyGovernanceCapability,
    SentinelPolicyGovernanceLifecycle,
    SentinelPolicyGovernanceRecord,
    SentinelPolicyGovernanceScope,
    SentinelPolicyGovernanceSourceResult,
    SentinelPolicyGovernanceSourceStatus,
    SentinelPolicyGovernanceTrustLevel,
    SentinelPolicyTrustAnchorDescriptor,
    SentinelPolicyTrustAnchorType,
)
from app.services.sentinel_policy_governance import (
    SentinelPolicyGovernanceBoundary,
    governance_binding_codes,
)
from tests.test_demo_execution_readiness import NOW
from tests.test_sentinel_policy_authority import _test_policy_record


def _signed_state(policy, **artifact_changes):
    values = {
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "policy_fingerprint": policy.fingerprint,
        "approver_id": "TEST-GOVERNANCE-APPROVER",
        "mechanism": SentinelPolicyApprovalMechanism.DETACHED_SIGNATURE,
        "verification_key_id": "TEST-ED25519-KEY",
        "signature_algorithm": "ed25519",
        "proof": "synthetic-signature-shaped-test-value",
        "approved_at": NOW - timedelta(minutes=1),
        "effective_at": NOW - timedelta(minutes=2),
        "expires_at": NOW + timedelta(minutes=10),
        "scope": SentinelPolicyApprovalScope(
            account_ids=(policy.allowed_account.account_id,),
            execution_modes=(policy.mode.value,),
            canonical_symbols=(policy.instrument.canonical_symbol,),
            risk_currencies=(policy.risk_currency,),
        ),
    }
    values.update(artifact_changes)
    artifact = SentinelPolicyApprovalArtifact(**values)
    return SentinelPolicyApprovalState(
        artifact=artifact,
        registered_at=NOW - timedelta(minutes=1),
        lifecycle_changed_at=NOW - timedelta(minutes=1),
    )


def _governance(policy, **changes):
    values = {
        "trust_level": SentinelPolicyGovernanceTrustLevel.INDEPENDENTLY_VERIFIED,
        "lifecycle": SentinelPolicyGovernanceLifecycle.ACTIVE,
        "lifecycle_version": 1,
        "source_id": "TEST-GOVERNANCE-SOURCE",
        "source_version": "V1",
        "approver_id": "TEST-GOVERNANCE-APPROVER",
        "organizational_role": "TEST-POLICY-REVIEWER",
        "capabilities": (
            SentinelPolicyGovernanceCapability.APPROVE_POLICY,
            SentinelPolicyGovernanceCapability.REVOKE_POLICY,
            SentinelPolicyGovernanceCapability.SUPERSEDE_POLICY,
        ),
        "scope": SentinelPolicyGovernanceScope(
            policy_ids=(policy.policy_id,),
            policy_versions=(policy.policy_version,),
            execution_modes=(policy.mode.value,),
            account_ids=(policy.allowed_account.account_id,),
            canonical_symbols=(policy.instrument.canonical_symbol,),
        ),
        "required_approval_mechanism": SentinelPolicyApprovalMechanism.DETACHED_SIGNATURE,
        "trust_anchor": SentinelPolicyTrustAnchorDescriptor(
            anchor_type=SentinelPolicyTrustAnchorType.ED25519_PUBLIC_KEY,
            anchor_id="TEST-ED25519-KEY",
            algorithm="ed25519",
            public_identity_fingerprint="a" * 64,
            provenance_reference="synthetic-test-provenance",
            independently_established_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
        ),
        "valid_from": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
        "registered_at": NOW - timedelta(hours=1),
        "lifecycle_changed_at": NOW - timedelta(hours=1),
        "audit_requirements": ("independent_review", "revocation_record", "retention"),
    }
    values.update(changes)
    return SentinelPolicyGovernanceRecord(**values)


def test_unconfigured_governance_names_no_approver_or_trust_anchor() -> None:
    record = SentinelPolicyGovernanceRecord(
        trust_level=SentinelPolicyGovernanceTrustLevel.UNCONFIGURED,
        lifecycle=SentinelPolicyGovernanceLifecycle.UNCONFIGURED,
        lifecycle_version=1,
        source_id="UNCONFIGURED",
        source_version="V1",
        registered_at=NOW,
        lifecycle_changed_at=NOW,
        unconfigured_reason="organizational approver and trust anchor have not been established",
    )
    assert record.approver_id is None
    assert record.trust_anchor is None
    assert len(record.governance_record_id) == 64
    with pytest.raises(ValidationError):
        record.approver_id = "invented"  # type: ignore[misc]


def test_unconfigured_record_rejects_an_invented_approver_or_authority() -> None:
    with pytest.raises(ValidationError, match="cannot name"):
        SentinelPolicyGovernanceRecord(
            trust_level="unconfigured",
            lifecycle="unconfigured",
            lifecycle_version=1,
            source_id="UNCONFIGURED",
            source_version="V1",
            approver_id="INVENTED",
            registered_at=NOW,
            lifecycle_changed_at=NOW,
            unconfigured_reason="missing real decision",
        )


def test_configured_governance_requires_approver_scope_revocation_and_audit() -> None:
    policy = _test_policy_record().policy
    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["approver_id"] = None
    with pytest.raises(ValidationError, match="requires approver"):
        SentinelPolicyGovernanceRecord.model_validate(payload)
    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["capabilities"] = ("approve_policy",)
    with pytest.raises(ValidationError, match="revocation"):
        SentinelPolicyGovernanceRecord.model_validate(payload)
    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["audit_requirements"] = ()
    with pytest.raises(ValidationError, match="audit"):
        SentinelPolicyGovernanceRecord.model_validate(payload)


def test_independent_governance_requires_a_non_test_trust_anchor() -> None:
    policy = _test_policy_record().policy
    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["trust_anchor"] = None
    with pytest.raises(ValidationError, match="trust anchor"):
        SentinelPolicyGovernanceRecord.model_validate(payload)
    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["required_approval_mechanism"] = "test_fixture"
    with pytest.raises(ValidationError, match="test fixture"):
        SentinelPolicyGovernanceRecord.model_validate(payload)


def test_exact_governance_binding_is_deterministic_but_not_authority() -> None:
    policy = _test_policy_record().policy
    governance = _governance(policy)
    approval = _signed_state(policy)
    assert governance_binding_codes(
        governance=governance, approval=approval, policy=policy, evaluated_at=NOW
    ) == ()
    assert _governance(policy).record_fingerprint == governance.record_fingerprint


@pytest.mark.parametrize(
    ("governance_changes", "artifact_changes", "code"),
    [
        ({"approver_id": "OTHER"}, {}, "SENTINEL_POLICY_GOVERNANCE_APPROVER_MISMATCH"),
        ({}, {"verification_key_id": "WRONG-KEY"}, "SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_MISMATCH"),
        ({"expires_at": NOW}, {}, "SENTINEL_POLICY_GOVERNANCE_NOT_EFFECTIVE"),
        (
            {"lifecycle": "revoked", "lifecycle_version": 2, "revoked_reason": "withdrawn", "lifecycle_changed_at": NOW},
            {},
            "SENTINEL_POLICY_GOVERNANCE_REVOKED",
        ),
        (
            {"lifecycle": "superseded", "lifecycle_version": 2, "superseded_by_record_id": "b" * 64, "lifecycle_changed_at": NOW},
            {},
            "SENTINEL_POLICY_GOVERNANCE_SUPERSEDED",
        ),
    ],
)
def test_governance_binding_rejects_unauthorized_stale_or_terminal_claims(
    governance_changes, artifact_changes, code
) -> None:
    policy = _test_policy_record().policy
    codes = governance_binding_codes(
        governance=_governance(policy, **governance_changes),
        approval=_signed_state(policy, **artifact_changes),
        policy=policy,
        evaluated_at=NOW,
    )
    assert code in codes


def test_out_of_scope_governance_and_old_policy_version_are_rejected() -> None:
    policy = _test_policy_record().policy
    scope = _governance(policy).scope.model_copy(
        update={"policy_versions": ("OTHER-VERSION",), "canonical_symbols": ("XAGUSD",)}
    )
    codes = governance_binding_codes(
        governance=_governance(policy, scope=scope),
        approval=_signed_state(policy),
        policy=policy,
        evaluated_at=NOW,
    )
    assert "SENTINEL_POLICY_GOVERNANCE_SCOPE_MISMATCH" in codes


def test_typed_source_results_reject_ambiguous_or_mismatched_records() -> None:
    policy = _test_policy_record().policy
    with pytest.raises(ValidationError, match="one record"):
        SentinelPolicyGovernanceSourceResult(
            status=SentinelPolicyGovernanceSourceStatus.AVAILABLE,
            source_id="SOURCE",
            requested_policy_id=policy.policy_id,
            requested_policy_version=policy.policy_version,
            resolved_at=NOW,
        )
    state = _signed_state(policy)
    with pytest.raises(ValidationError, match="exact request"):
        SentinelPolicyApprovalSourceResult(
            status=SentinelPolicyApprovalSourceStatus.AVAILABLE,
            source_id="SOURCE",
            requested_policy_id=policy.policy_id,
            requested_policy_version="WRONG",
            requested_artifact_id=state.artifact.approval_artifact_id,
            resolved_at=NOW,
            state=state,
        )


def test_default_sources_are_typed_unavailable_and_production_remains_blocked() -> None:
    policy = _test_policy_record().policy
    result = SentinelPolicyGovernanceBoundary().assess(
        approval=_signed_state(policy),
        policy=policy,
        policy_source_configured=True,
        evaluated_at=NOW,
    )
    assert result.status.value == "unavailable"
    assert result.reason_codes == (
        "SENTINEL_POLICY_APPROVAL_SOURCE_UNAVAILABLE",
        "SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_UNAVAILABLE",
        "SENTINEL_POLICY_GOVERNANCE_UNCONFIGURED",
    )
    assert result.readiness.policy_source_configured is True
    assert result.readiness.governance_contract_configured is False
    assert result.readiness.approval_source_configured is False
    assert result.readiness.revocation_status_available is False
    assert result.readiness.production_policy_resolution_available is False


def test_arbitrary_protocol_sources_cannot_masquerade_as_authority() -> None:
    policy = _test_policy_record().policy

    class CallerGovernanceSource:
        source_id = "CALLER"

        def resolve(self, **kwargs):
            return SentinelPolicyGovernanceSourceResult(
                status="available", source_id=self.source_id,
                requested_policy_id=kwargs["policy_id"], requested_policy_version=kwargs["policy_version"],
                resolved_at=kwargs["evaluated_at"], record=_governance(policy),
            )

    result = SentinelPolicyGovernanceBoundary(
        governance_source=CallerGovernanceSource()
    ).assess(
        approval=_signed_state(policy), policy=policy,
        policy_source_configured=True, evaluated_at=NOW,
    )
    assert "SENTINEL_POLICY_GOVERNANCE_SOURCE_UNTRUSTED" in result.reason_codes
    assert result.readiness.production_policy_resolution_available is False


def test_governance_contract_rejects_private_key_or_approval_boolean_fields() -> None:
    policy = _test_policy_record().policy
    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["private_key"] = "must-not-exist"
    payload["approved"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        SentinelPolicyGovernanceRecord.model_validate(payload)

    payload = _governance(policy).model_dump(exclude_computed_fields=True)
    payload["audit_requirements"] = ("password=must-not-be-stored",)
    with pytest.raises(ValidationError, match="safe identifiers"):
        SentinelPolicyGovernanceRecord.model_validate(payload)
