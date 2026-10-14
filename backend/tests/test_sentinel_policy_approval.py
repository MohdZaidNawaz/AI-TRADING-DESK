"""Independent policy-approval boundary tests; no broker or network I/O."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalArtifact,
    SentinelPolicyApprovalLifecycle,
    SentinelPolicyApprovalMechanism,
    SentinelPolicyApprovalReadiness,
    SentinelPolicyApprovalScope,
    SentinelPolicyApprovalState,
    SentinelPolicyApprovalVerification,
    SentinelPolicyApprovalVerificationStatus,
)
from app.models.sentinel_policy_authority import SentinelPolicyResolutionStatus
from app.models.sentinel_policy_authority import (
    SentinelPolicyAuthorityLevel,
    SentinelPolicyLifecycle,
    SentinelPolicyProvenance,
    SentinelPolicySourceKind,
    SentinelPortfolioRiskPolicyRecord,
)
from app.services.sentinel_policy_approval import (
    UnavailableSentinelPolicyApprovalVerifier,
    approval_binding_codes,
)
from app.services.sentinel_policy_authority import (
    SentinelPortfolioRiskPolicyVerifier,
    TestOnlySentinelPolicySource,
)
from tests.test_demo_execution_readiness import NOW
from tests.test_sentinel_policy_authority import (
    _test_approval_state,
    _test_policy_record,
    _test_policy_verifier,
)


def test_artifact_is_immutable_deterministic_and_has_no_approval_boolean() -> None:
    record = _test_policy_record()
    left = record.approval
    right = _test_approval_state(record.policy)
    assert left is not None
    assert left.artifact.approval_artifact_id == right.artifact.approval_artifact_id
    assert left.artifact.canonical_payload_fingerprint == right.artifact.canonical_payload_fingerprint
    with pytest.raises(ValidationError):
        left.artifact.proof = "changed"  # type: ignore[misc]
    payload = left.artifact.model_dump(exclude_computed_fields=True)
    payload["approved"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        SentinelPolicyApprovalArtifact.model_validate(payload)


def test_artifact_rejects_incomplete_or_fake_signature_metadata() -> None:
    policy = _test_policy_record().policy
    state = _test_approval_state(policy)
    base = state.artifact.model_dump(exclude_computed_fields=True)
    base.update({"mechanism": "detached_signature", "verification_key_id": None, "signature_algorithm": None})
    with pytest.raises(ValidationError, match="key and algorithm"):
        SentinelPolicyApprovalArtifact.model_validate(base)
    base["verification_key_id"] = "unknown-key"
    base["signature_algorithm"] = "rsa-sha1"
    with pytest.raises(ValidationError):
        SentinelPolicyApprovalArtifact.model_validate(base)


@pytest.mark.parametrize("field", ["policy_id", "policy_version", "policy_fingerprint"])
def test_artifact_binding_rejects_wrong_policy_identity_or_content(field: str) -> None:
    record = _test_policy_record()
    change = "0" * 64 if field == "policy_fingerprint" else "WRONG"
    with pytest.raises(ValidationError, match="bind"):
        _test_policy_record(record.policy, approval=_test_approval_state(record.policy, **{field: change}))


def test_policy_modification_invalidates_previous_approval() -> None:
    record = _test_policy_record()
    altered = record.policy.model_copy(update={"max_order_notional": record.policy.max_order_notional + 1})
    assert "SENTINEL_POLICY_APPROVAL_FINGERPRINT_MISMATCH" in approval_binding_codes(
        approval=record.approval,  # type: ignore[arg-type]
        policy=altered,
        evaluated_at=NOW,
    )


def test_scope_and_effective_window_fail_closed() -> None:
    policy = _test_policy_record().policy
    wrong_scope = SentinelPolicyApprovalScope(
        account_ids=(policy.allowed_account.account_id,),
        execution_modes=(policy.mode.value,),
        canonical_symbols=("XAGUSD",),
        risk_currencies=(policy.risk_currency,),
    )
    state = _test_approval_state(policy, scope=wrong_scope)
    assert approval_binding_codes(approval=state, policy=policy, evaluated_at=NOW) == (
        "SENTINEL_POLICY_APPROVAL_SCOPE_MISMATCH",
    )
    future = _test_approval_state(
        policy,
        approved_at=NOW + timedelta(minutes=1),
        effective_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=2),
    )
    assert "SENTINEL_POLICY_APPROVAL_NOT_EFFECTIVE" in approval_binding_codes(
        approval=future, policy=policy, evaluated_at=NOW
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"lifecycle": SentinelPolicyApprovalLifecycle.REVOKED, "lifecycle_version": 2,
             "revoked_reason": "governance-revoked", "lifecycle_changed_at": NOW},
            "SENTINEL_POLICY_APPROVAL_REVOKED",
        ),
        (
            {"lifecycle": SentinelPolicyApprovalLifecycle.SUPERSEDED, "lifecycle_version": 2,
             "superseded_by_artifact_id": "1" * 64, "lifecycle_changed_at": NOW},
            "SENTINEL_POLICY_APPROVAL_SUPERSEDED",
        ),
    ],
)
def test_revoked_or_superseded_approval_cannot_resolve(changes, expected: str) -> None:
    policy = _test_policy_record().policy
    record = _test_policy_record(policy, approval=_test_approval_state(policy, **changes))
    result = _test_policy_verifier((record,)).resolve(candidate=policy, evaluated_at=NOW)
    assert result.status is SentinelPolicyResolutionStatus.REJECTED
    assert expected in result.reason_codes


def test_unauthorized_approver_and_invalid_proof_are_rejected() -> None:
    policy = _test_policy_record().policy
    for state, code in (
        (_test_approval_state(policy, approver_id="UNAUTHORIZED"), "SENTINEL_POLICY_APPROVER_UNAUTHORIZED"),
        (_test_approval_state(policy, proof="signature-shaped-but-untrusted"), "SENTINEL_POLICY_APPROVAL_PROOF_INVALID"),
    ):
        result = _test_policy_verifier((_test_policy_record(policy, approval=state),)).resolve(
            candidate=policy, evaluated_at=NOW
        )
        assert result.status is SentinelPolicyResolutionStatus.REJECTED
        assert code in result.reason_codes


def test_missing_trust_anchor_is_typed_unavailable_and_non_authorizing() -> None:
    record = _test_policy_record()
    result = UnavailableSentinelPolicyApprovalVerifier().verify(
        approval=record.approval,  # type: ignore[arg-type]
        policy=record.policy,
        policy_source_configured=True,
        evaluated_at=NOW,
    )
    assert result.status is SentinelPolicyApprovalVerificationStatus.UNAVAILABLE
    assert result.reason_codes == ("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_UNAVAILABLE",)
    assert result.readiness.policy_source_configured is True
    assert result.readiness.trust_anchor_configured is False
    assert result.readiness.production_policy_resolution_available is False
    assert result.execution_authority is False


def test_authoritative_result_cannot_be_constructed_without_all_readiness_proofs() -> None:
    with pytest.raises(ValidationError, match="readiness"):
        SentinelPolicyApprovalVerification(
            status=SentinelPolicyApprovalVerificationStatus.AUTHORITATIVE,
            artifact_id="1" * 64,
            artifact_fingerprint="2" * 64,
            approver_id="APPROVER",
            verified_at=NOW,
            readiness=SentinelPolicyApprovalReadiness(),
        )


def test_verified_result_requires_complete_binding_readiness() -> None:
    with pytest.raises(ValidationError, match="complete source"):
        SentinelPolicyApprovalVerification(
            status=SentinelPolicyApprovalVerificationStatus.TEST_ONLY_VERIFIED,
            artifact_id="1" * 64,
            artifact_fingerprint="2" * 64,
            approver_id="APPROVER",
            verified_at=NOW,
            readiness=SentinelPolicyApprovalReadiness(
                policy_source_configured=True,
                approval_artifact_available=True,
            ),
        )


def test_malformed_or_mismatched_test_verifier_result_fails_closed() -> None:
    record = _test_policy_record()

    class MalformedVerifier:
        def verify(self, **kwargs):
            del kwargs
            return {"status": "test_only_verified"}

    malformed = SentinelPortfolioRiskPolicyVerifier.for_test(
        (record,), approval_verifier=MalformedVerifier()
    ).resolve(candidate=record.policy, evaluated_at=NOW)
    assert malformed.status is SentinelPolicyResolutionStatus.REJECTED
    assert "SENTINEL_POLICY_APPROVAL_VERIFIER_RESULT_INVALID" in malformed.reason_codes

    class MismatchedVerifier:
        def verify(self, **kwargs):
            del kwargs
            return SentinelPolicyApprovalVerification(
                status=SentinelPolicyApprovalVerificationStatus.TEST_ONLY_VERIFIED,
                artifact_id="1" * 64,
                artifact_fingerprint="2" * 64,
                approver_id="OTHER-APPROVER",
                verified_at=NOW,
                readiness=SentinelPolicyApprovalReadiness(
                    policy_source_configured=True,
                    approval_artifact_available=True,
                    approver_verified=True,
                    policy_fingerprint_verified=True,
                    approval_lifecycle_valid=True,
                    approval_scope_valid=True,
                ),
            )

    mismatched = SentinelPortfolioRiskPolicyVerifier.for_test(
        (record,), approval_verifier=MismatchedVerifier()
    ).resolve(candidate=record.policy, evaluated_at=NOW)
    assert mismatched.status is SentinelPolicyResolutionStatus.REJECTED
    assert "SENTINEL_POLICY_APPROVAL_VERIFICATION_LINEAGE_MISMATCH" in mismatched.reason_codes


def test_test_provider_cannot_be_selected_through_production_construction() -> None:
    record = _test_policy_record()
    result = SentinelPortfolioRiskPolicyVerifier(
        TestOnlySentinelPolicySource((record,)),
        approval_verifier=UnavailableSentinelPolicyApprovalVerifier(),
    ).resolve(candidate=record.policy, evaluated_at=NOW)
    assert result.status is SentinelPolicyResolutionStatus.UNAVAILABLE
    assert result.reason_codes == ("SENTINEL_RISK_POLICY_SOURCE_UNTRUSTED",)


def test_signature_shaped_claim_and_trusted_label_are_not_a_trust_anchor() -> None:
    policy = _test_policy_record().policy
    artifact = SentinelPolicyApprovalArtifact(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        approver_id="CALLER-CLAIMED-APPROVER",
        mechanism=SentinelPolicyApprovalMechanism.DETACHED_SIGNATURE,
        verification_key_id="CALLER-CLAIMED-KEY",
        signature_algorithm="ed25519",
        proof="signature-shaped-but-not-verified",
        approved_at=NOW - timedelta(minutes=1),
        effective_at=NOW - timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=10),
        scope=_test_approval_state(policy).artifact.scope,
    )
    record = SentinelPortfolioRiskPolicyRecord(
        policy=policy,
        lifecycle=SentinelPolicyLifecycle.ACTIVE,
        lifecycle_version=1,
        authority_level=SentinelPolicyAuthorityLevel.INDEPENDENTLY_APPROVED,
        provenance=SentinelPolicyProvenance(
            source_id="CALLER-CLAIMED-GOVERNANCE",
            source_kind=SentinelPolicySourceKind.TRUSTED_GOVERNANCE,
            source_version="V1",
        ),
        approval=SentinelPolicyApprovalState(
            artifact=artifact,
            registered_at=NOW - timedelta(minutes=1),
            lifecycle_changed_at=NOW - timedelta(minutes=1),
        ),
        registered_at=NOW - timedelta(minutes=1),
        lifecycle_changed_at=NOW - timedelta(minutes=1),
    )

    class CallerClaimedSource:
        source_id = "caller-claimed-source"

        def resolve(self, *, policy_id, policy_version):
            del policy_id, policy_version
            return record

    result = SentinelPortfolioRiskPolicyVerifier(CallerClaimedSource()).resolve(
        candidate=policy, evaluated_at=NOW
    )
    assert result.status is SentinelPolicyResolutionStatus.UNAVAILABLE
    assert result.reason_codes == ("SENTINEL_RISK_POLICY_SOURCE_UNTRUSTED",)
    assert result.approval_readiness.production_policy_resolution_available is False


def test_duplicate_is_idempotent_but_conflicting_approval_is_rejected() -> None:
    record = _test_policy_record()
    source = TestOnlySentinelPolicySource((record, record))
    assert source.resolve(
        policy_id=record.policy.policy_id, policy_version=record.policy.policy_version
    ) == record
    conflict = _test_policy_record(
        record.policy,
        approval=_test_approval_state(record.policy, proof="PYTEST-ONLY-VERIFIED-PROOF-CHANGED"),
    )
    with pytest.raises(ValueError, match="conflicting"):
        TestOnlySentinelPolicySource((record, conflict))


def test_old_approval_replay_cannot_authorize_replacement_policy_version() -> None:
    old = _test_policy_record()
    replacement = old.policy.model_copy(update={"policy_version": "V2"})
    assert "SENTINEL_POLICY_APPROVAL_IDENTITY_MISMATCH" in approval_binding_codes(
        approval=old.approval,  # type: ignore[arg-type]
        policy=replacement,
        evaluated_at=NOW,
    )
