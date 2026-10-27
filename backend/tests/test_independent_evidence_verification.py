"""Phase 22 offline cryptographic evidence-verification tests."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
import pytest

from app.models.dependency_acceptance import (
    DependencyAcceptanceContext,
    EvidenceProvenance,
    EvidenceRevocationState,
    EvidenceVerificationResult,
    ExternalDependencyEvidenceEnvelope,
    ExternalDependencyId,
    HumanApprovalPayload,
    IntegrationEnvironment,
    TrustDependencyPayload,
)
from app.models.independent_evidence_verification import (
    BindingVerificationStatus,
    ConfiguredKeyLifecycle,
    EvidenceTrustConfiguration,
    EvidenceVerificationDisposition,
    EvidenceVerificationReason,
    EvidenceVerificationRequest,
    FreshnessVerificationStatus,
    IssuerAuthenticationStatus,
    LifecycleVerificationStatus,
    ReplayEvaluationStatus,
    SignatureVerificationStatus,
    SignedDependencyEvidence,
    StructuralVerificationStatus,
    canonical_dependency_evidence_payload,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.services.dependency_acceptance import PHASE21_POLICY_VERSION
from app.services.independent_evidence_verification import (
    OfflineIndependentEvidenceVerifier,
)


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
FP = "a" * 64
OTHER_FP = "b" * 64
COMMIT = "1" * 40


def _key_material():
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private, public_pem


def _context(**changes) -> DependencyAcceptanceContext:
    values = {
        "environment": IntegrationEnvironment.SYNTHETIC_TEST,
        "subject_id": "friday.release",
        "request_id": "request/phase22",
        "challenge_id": "challenge/phase22",
        "scope_fingerprint": FP,
        "acceptance_policy_version": PHASE21_POLICY_VERSION,
        "commit_hash": COMMIT,
        "evaluated_at": NOW,
    }
    values.update(changes)
    return DependencyAcceptanceContext(**values)


def _evidence(
    dependency_id: ExternalDependencyId = ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
    **changes,
) -> ExternalDependencyEvidenceEnvelope:
    issued_at = changes.pop("issued_at", NOW - timedelta(minutes=1))
    expires_at = changes.pop("expires_at", NOW + timedelta(minutes=5))
    if dependency_id is ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL:
        payload = changes.pop(
            "payload",
            HumanApprovalPayload(
                approver_identity_fingerprint=FP,
                approver_role="synthetic.approver",
                approved_scope_fingerprint=FP,
                target_commit=COMMIT,
                policy_version=PHASE21_POLICY_VERSION,
                issued_at=issued_at,
                expires_at=expires_at,
                revocation_state=EvidenceRevocationState.ACTIVE,
                decision_id="decision/phase22",
                decision="approved",
                evidence_fingerprint=OTHER_FP,
            ),
        )
        evidence_type = "human_approval_decision"
        commit_hash = COMMIT
    else:
        payload_type = {
            ExternalDependencyId.INDEPENDENT_TRUST_ROOT: "trust_root_provisioning",
            ExternalDependencyId.LIFECYCLE_REVOCATION_AUTHORITY: "lifecycle_status",
            ExternalDependencyId.MONOTONIC_ROLLBACK_WITNESS: "rollback_witness_confirmation",
        }[dependency_id]
        payload = changes.pop(
            "payload",
            TrustDependencyPayload(
                payload_type=payload_type,
                lineage_fingerprint=FP,
                authority_identity_fingerprint=OTHER_FP,
            ),
        )
        evidence_type = payload_type
        commit_hash = changes.pop("commit_hash", COMMIT)
    values = {
        "evidence_id": f"fixture/{dependency_id.value}",
        "dependency_id": dependency_id,
        "evidence_type": evidence_type,
        "issuer_id": "fixture.issuer",
        "subject_id": "friday.release",
        "environment": IntegrationEnvironment.SYNTHETIC_TEST,
        "request_id": "request/phase22",
        "challenge_id": "challenge/phase22",
        "scope_fingerprint": FP,
        "payload_fingerprint": payload.fingerprint,
        "payload": payload,
        "signature_fingerprint": FP,
        "trust_chain_fingerprint": OTHER_FP,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "verified_at": issued_at,
        "source_sequence": 1,
        "previous_source_sequence": 0,
        "provenance": EvidenceProvenance.SYNTHETIC_FIXTURE,
        "verification_result": EvidenceVerificationResult.SYNTHETIC_ONLY,
        "revocation_state": EvidenceRevocationState.ACTIVE,
        "synthetic": True,
        "acceptance_policy_version": PHASE21_POLICY_VERSION,
        "commit_hash": commit_hash,
    }
    values.update(changes)
    return ExternalDependencyEvidenceEnvelope(**values)


def _configuration(public_pem: str, **changes) -> EvidenceTrustConfiguration:
    root = changes.pop(
        "trust_anchor",
        ConfiguredEd25519TrustAnchor(
            authority_id="FIXTURE.ISSUER",
            anchor_id="PHASE22.KEY.1",
            anchor_version="1",
            public_key_pem=public_pem,
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            provenance_reference="synthetic-test-fixture",
        ),
    )
    values = {
        "configuration_id": "PHASE22.TEST.CONFIG",
        "issuer_id": "FIXTURE.ISSUER",
        "key_id": "PHASE22.KEY.1",
        "key_generation": 1,
        "trust_anchor": root,
        "lifecycle": ConfiguredKeyLifecycle.ACTIVE,
        "permitted_dependencies": tuple(ExternalDependencyId),
        "permitted_environments": (
            IntegrationEnvironment.LOCAL_DEVELOPMENT,
            IntegrationEnvironment.SYNTHETIC_TEST,
        ),
        "permitted_scope_fingerprints": (FP,),
    }
    values.update(changes)
    return EvidenceTrustConfiguration(**values)


def _signed(
    private: Ed25519PrivateKey,
    configuration: EvidenceTrustConfiguration,
    evidence: ExternalDependencyEvidenceEnvelope | None = None,
    **changes,
) -> SignedDependencyEvidence:
    values = {
        "evidence": evidence or _evidence(),
        "key_id": configuration.key_id,
        "key_generation": configuration.key_generation,
        "verification_root_version": configuration.trust_anchor.anchor_version,
        "verification_root_fingerprint": configuration.trust_anchor.public_key_fingerprint,
        "signature_base64": base64.b64encode(b"\x00" * 64).decode("ascii"),
    }
    values.update(changes)
    unsigned = SignedDependencyEvidence(**values)
    return unsigned.model_copy(
        update={
            "signature_base64": base64.b64encode(
                private.sign(canonical_dependency_evidence_payload(unsigned))
            ).decode("ascii")
        }
    )


def _request(signed: SignedDependencyEvidence | None, **changes) -> EvidenceVerificationRequest:
    values = {"context": _context(), "signed_evidence": signed}
    values.update(changes)
    return EvidenceVerificationRequest(**values)


def _assert_non_authorizing(report) -> None:
    assert report.independent_verification_available is False
    assert report.independently_verified is False
    assert report.operationally_accepted is False
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
    assert report.authorizes_execution is False


def test_valid_synthetic_signature_is_verified_but_never_authoritative() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration)), evaluated_at=NOW
    )

    assert report.disposition is EvidenceVerificationDisposition.CONFIGURED_KEY_VERIFIED_NON_AUTHORITATIVE
    assert report.structural_status is StructuralVerificationStatus.VALID
    assert report.signature_status is SignatureVerificationStatus.VALID
    assert report.issuer_status is IssuerAuthenticationStatus.CONFIGURED_SYNTHETIC_KEY
    assert report.binding_status is BindingVerificationStatus.VALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert EvidenceVerificationReason.SYNTHETIC_TRUST_ROOT_NON_AUTHORITATIVE in report.reason_codes
    assert EvidenceVerificationReason.INDEPENDENT_LIFECYCLE_VERIFICATION_UNAVAILABLE in report.reason_codes
    assert EvidenceVerificationReason.EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE in report.reason_codes
    assert report.independently_verified is False
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
    assert report.authorizes_execution is False


def test_tampered_payload_and_signature_are_rejected_by_real_verifier() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    original = _signed(private, configuration)
    altered_payload = original.evidence.payload.model_copy(update={"lineage_fingerprint": "c" * 64})
    altered_evidence = original.evidence.model_copy(
        update={
            "payload": altered_payload,
            "payload_fingerprint": altered_payload.fingerprint,
        }
    )
    tampered_payload = original.model_copy(update={"evidence": altered_evidence})
    tampered_signature = original.model_copy(
        update={"signature_base64": base64.b64encode(b"x" * 64).decode("ascii")}
    )

    for signed in (tampered_payload, tampered_signature):
        report = OfflineIndependentEvidenceVerifier(configuration).verify(
            request=_request(signed), evaluated_at=NOW
        )
        assert report.disposition is EvidenceVerificationDisposition.SIGNATURE_INVALID
        assert report.signature_status is SignatureVerificationStatus.INVALID
        assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE


def test_malformed_signature_unsupported_algorithm_and_key_type_fail_at_models() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    signed = _signed(private, configuration)
    values = signed.model_dump(exclude_computed_fields=True)
    values["signature_base64"] = "not-base64"
    with pytest.raises(ValidationError):
        SignedDependencyEvidence.model_validate(values)
    values = signed.model_dump(exclude_computed_fields=True)
    values["algorithm"] = "RSA_SHA256"
    with pytest.raises(ValidationError):
        SignedDependencyEvidence.model_validate(values)

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    with pytest.raises(ValidationError, match="Ed25519"):
        ConfiguredEd25519TrustAnchor(
            authority_id="FIXTURE.ISSUER",
            anchor_id="PHASE22.KEY.1",
            anchor_version="1",
            public_key_pem=rsa_pem,
            valid_from=NOW,
            provenance_reference="synthetic-test-fixture",
        )


def test_embedded_key_cannot_create_trust_without_configuration() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    signed = _signed(private, configuration, presented_public_key_pem=public)
    report = OfflineIndependentEvidenceVerifier().verify(
        request=_request(signed), evaluated_at=NOW
    )

    assert report.disposition is EvidenceVerificationDisposition.UNAVAILABLE
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE
    assert EvidenceVerificationReason.VERIFIER_CONFIGURATION_UNAVAILABLE in report.reason_codes
    assert EvidenceVerificationReason.EMBEDDED_PUBLIC_KEY_NOT_TRUSTED in report.reason_codes


@pytest.mark.parametrize(
    ("configuration_changes", "signed_changes", "reason"),
    (
        ({"issuer_id": "OTHER.ISSUER"}, {}, EvidenceVerificationReason.ISSUER_IDENTITY_MISMATCH),
        ({}, {"key_id": "OTHER.KEY"}, EvidenceVerificationReason.KEY_IDENTITY_MISMATCH),
        ({}, {"key_generation": 2}, EvidenceVerificationReason.KEY_GENERATION_MISMATCH),
        ({}, {"verification_root_version": "2"}, EvidenceVerificationReason.ROOT_VERSION_MISMATCH),
        ({}, {"verification_root_fingerprint": "c" * 64}, EvidenceVerificationReason.ROOT_FINGERPRINT_MISMATCH),
    ),
)
def test_unknown_issuer_rotated_key_and_root_mismatches_fail_closed(
    configuration_changes, signed_changes, reason
) -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    if configuration_changes:
        if "issuer_id" in configuration_changes:
            other_root = configuration.trust_anchor.model_copy(
                update={"authority_id": configuration_changes["issuer_id"]}
            )
            configuration = _configuration(
                public,
                trust_anchor=other_root,
                **configuration_changes,
            )
    signed = _signed(private, configuration, **signed_changes)
    if reason is EvidenceVerificationReason.ISSUER_IDENTITY_MISMATCH:
        signed = _signed(private, configuration, evidence=_evidence(issuer_id="fixture.issuer"))
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(signed), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.BINDING_INVALID
    assert report.binding_status is BindingVerificationStatus.INVALID
    assert report.issuer_status is IssuerAuthenticationStatus.MISMATCH
    assert reason in report.reason_codes
    assert report.independently_verified is False


@pytest.mark.parametrize(
    ("lifecycle", "reason", "expected"),
    (
        (ConfiguredKeyLifecycle.REVOKED, EvidenceVerificationReason.TRUST_ROOT_REVOKED, LifecycleVerificationStatus.REVOKED),
        (ConfiguredKeyLifecycle.SUPERSEDED, EvidenceVerificationReason.TRUST_ROOT_SUPERSEDED, LifecycleVerificationStatus.SUPERSEDED),
        (ConfiguredKeyLifecycle.COMPROMISED, EvidenceVerificationReason.TRUST_ROOT_COMPROMISED, LifecycleVerificationStatus.COMPROMISED),
        (ConfiguredKeyLifecycle.UNKNOWN, EvidenceVerificationReason.TRUST_ROOT_LIFECYCLE_UNKNOWN, LifecycleVerificationStatus.UNKNOWN),
    ),
)
def test_configured_root_lifecycle_never_silently_trusts_rotation(lifecycle, reason, expected) -> None:
    private, public = _key_material()
    configuration = _configuration(public, lifecycle=lifecycle)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration)), evaluated_at=NOW
    )
    assert reason in report.reason_codes
    assert report.binding_status is BindingVerificationStatus.VALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE
    assert report.lifecycle_status is expected
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    assert report.disposition is (
        EvidenceVerificationDisposition.UNAVAILABLE
        if lifecycle is ConfiguredKeyLifecycle.UNKNOWN
        else EvidenceVerificationDisposition.REVOKED
    )
    _assert_non_authorizing(report)


def test_expired_configured_root_fails_closed() -> None:
    private, public = _key_material()
    expired_root = ConfiguredEd25519TrustAnchor(
        authority_id="FIXTURE.ISSUER",
        anchor_id="PHASE22.KEY.1",
        anchor_version="1",
        public_key_pem=public,
        valid_from=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(seconds=1),
        provenance_reference="synthetic-test-fixture",
    )
    configuration = _configuration(public, trust_anchor=expired_root)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration)), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.STALE
    assert report.binding_status is BindingVerificationStatus.VALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE
    assert EvidenceVerificationReason.TRUST_ROOT_NOT_CURRENT in report.reason_codes
    _assert_non_authorizing(report)


@pytest.mark.parametrize(
    ("configuration_changes", "dependency_id", "reason"),
    (
        (
            {"permitted_dependencies": (ExternalDependencyId.INDEPENDENT_TRUST_ROOT,)},
            ExternalDependencyId.LIFECYCLE_REVOCATION_AUTHORITY,
            EvidenceVerificationReason.DEPENDENCY_NOT_PERMITTED,
        ),
        (
            {"permitted_environments": (IntegrationEnvironment.LOCAL_DEVELOPMENT,)},
            ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
            EvidenceVerificationReason.ENVIRONMENT_NOT_PERMITTED,
        ),
        (
            {"permitted_scope_fingerprints": (OTHER_FP,)},
            ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
            EvidenceVerificationReason.SCOPE_NOT_PERMITTED,
        ),
    ),
)
def test_configured_dependency_environment_and_scope_permissions_are_enforced(
    configuration_changes, dependency_id, reason
) -> None:
    private, public = _key_material()
    configuration = _configuration(public, **configuration_changes)
    signed = _signed(private, configuration, evidence=_evidence(dependency_id))
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(signed), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.BINDING_INVALID
    assert report.binding_status is BindingVerificationStatus.INVALID
    assert report.issuer_status is IssuerAuthenticationStatus.MISMATCH
    assert reason in report.reason_codes


def test_malformed_copied_configuration_is_revalidated_and_not_trusted() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    malformed = configuration.model_copy(update={"synthetic_test_only": False})
    signed = _signed(private, configuration)
    report = OfflineIndependentEvidenceVerifier(malformed).verify(  # type: ignore[arg-type]
        request=_request(signed), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.UNAVAILABLE
    assert EvidenceVerificationReason.VERIFIER_CONFIGURATION_UNAVAILABLE in report.reason_codes


@pytest.mark.parametrize(
    ("evidence_changes", "context_changes", "reason"),
    (
        ({"subject_id": "other.release"}, {}, EvidenceVerificationReason.SUBJECT_BINDING_MISMATCH),
        ({"request_id": "request/other"}, {}, EvidenceVerificationReason.REQUEST_BINDING_MISMATCH),
        ({"challenge_id": "challenge/other"}, {}, EvidenceVerificationReason.CHALLENGE_BINDING_MISMATCH),
        ({"scope_fingerprint": OTHER_FP}, {}, EvidenceVerificationReason.SCOPE_BINDING_MISMATCH),
        ({"environment": IntegrationEnvironment.LOCAL_DEVELOPMENT}, {}, EvidenceVerificationReason.ENVIRONMENT_BINDING_MISMATCH),
        ({"acceptance_policy_version": "phase21/other"}, {}, EvidenceVerificationReason.POLICY_BINDING_MISMATCH),
    ),
)
def test_exact_context_bindings_are_required(evidence_changes, context_changes, reason) -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    signed = _signed(private, configuration, evidence=_evidence(**evidence_changes))
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(signed, context=_context(**context_changes)), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.BINDING_INVALID
    assert reason in report.reason_codes


def test_service_evaluation_time_must_match_bound_context() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(
            _signed(private, configuration),
            context=_context(evaluated_at=NOW - timedelta(seconds=1)),
        ),
        evaluated_at=NOW,
    )
    assert report.disposition is EvidenceVerificationDisposition.BINDING_INVALID
    assert EvidenceVerificationReason.EVALUATION_TIME_MISMATCH in report.reason_codes


def test_human_approval_requires_exact_target_commit() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    evidence = _evidence(ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL, commit_hash="2" * 40)
    signed = _signed(private, configuration, evidence=evidence)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(signed), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.BINDING_INVALID
    assert EvidenceVerificationReason.COMMIT_BINDING_MISMATCH in report.reason_codes


@pytest.mark.parametrize(
    ("evidence_changes", "status", "reason"),
    (
        ({"issued_at": NOW + timedelta(seconds=1), "verified_at": NOW + timedelta(seconds=1), "expires_at": NOW + timedelta(minutes=1)}, FreshnessVerificationStatus.FUTURE_DATED, EvidenceVerificationReason.EVIDENCE_FUTURE_DATED),
        ({"issued_at": NOW - timedelta(minutes=2), "verified_at": NOW - timedelta(minutes=2), "expires_at": NOW}, FreshnessVerificationStatus.EXPIRED, EvidenceVerificationReason.EVIDENCE_EXPIRED),
        ({"issued_at": NOW - timedelta(days=2), "verified_at": NOW - timedelta(days=2), "expires_at": NOW + timedelta(minutes=1)}, FreshnessVerificationStatus.STALE, EvidenceVerificationReason.EVIDENCE_STALE),
    ),
)
def test_future_expired_and_stale_evidence_fail_closed(evidence_changes, status, reason) -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    evidence = _evidence(**evidence_changes)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)), evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.STALE
    assert report.freshness_status is status
    assert reason in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED


@pytest.mark.parametrize(
    ("state", "status", "reason"),
    (
        (EvidenceRevocationState.REVOKED, LifecycleVerificationStatus.REVOKED, EvidenceVerificationReason.EVIDENCE_REVOKED),
        (EvidenceRevocationState.SUPERSEDED, LifecycleVerificationStatus.SUPERSEDED, EvidenceVerificationReason.EVIDENCE_SUPERSEDED),
        (EvidenceRevocationState.COMPROMISED, LifecycleVerificationStatus.COMPROMISED, EvidenceVerificationReason.EVIDENCE_COMPROMISED),
        (EvidenceRevocationState.UNKNOWN, LifecycleVerificationStatus.UNKNOWN, EvidenceVerificationReason.EVIDENCE_LIFECYCLE_UNKNOWN),
    ),
)
def test_evidence_lifecycle_claims_fail_closed(state, status, reason) -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    evidence = _evidence(revocation_state=state)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)), evaluated_at=NOW
    )
    assert report.lifecycle_status is status
    assert reason in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED


def test_stale_evidence_with_revocation_claim_preserves_both_failures() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    issued_at = NOW - timedelta(days=2)
    evidence = _evidence(
        issued_at=issued_at,
        verified_at=issued_at,
        expires_at=NOW + timedelta(minutes=1),
        revocation_state=EvidenceRevocationState.REVOKED,
    )
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)),
        evaluated_at=NOW,
    )

    assert report.disposition is EvidenceVerificationDisposition.STALE
    assert report.freshness_status is FreshnessVerificationStatus.STALE
    assert report.lifecycle_status is LifecycleVerificationStatus.REVOKED
    assert EvidenceVerificationReason.EVIDENCE_STALE in report.reason_codes
    assert EvidenceVerificationReason.EVIDENCE_REVOKED in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    _assert_non_authorizing(report)


def test_invalid_binding_precedes_untrusted_revocation_claim() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    evidence = _evidence(
        subject_id="other.release",
        revocation_state=EvidenceRevocationState.REVOKED,
    )
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)),
        evaluated_at=NOW,
    )

    assert report.disposition is EvidenceVerificationDisposition.BINDING_INVALID
    assert report.binding_status is BindingVerificationStatus.INVALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.REVOKED
    assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE
    assert EvidenceVerificationReason.SUBJECT_BINDING_MISMATCH in report.reason_codes
    assert EvidenceVerificationReason.EVIDENCE_REVOKED in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    _assert_non_authorizing(report)


def test_future_validity_start_prevents_configured_root_use() -> None:
    private, public = _key_material()
    future_root = ConfiguredEd25519TrustAnchor(
        authority_id="FIXTURE.ISSUER",
        anchor_id="PHASE22.KEY.1",
        anchor_version="1",
        public_key_pem=public,
        valid_from=NOW + timedelta(microseconds=1),
        expires_at=NOW + timedelta(days=1),
        provenance_reference="synthetic-test-fixture",
    )
    configuration = _configuration(public, trust_anchor=future_root)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration)), evaluated_at=NOW
    )

    assert report.disposition is EvidenceVerificationDisposition.STALE
    assert report.binding_status is BindingVerificationStatus.VALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE
    assert EvidenceVerificationReason.TRUST_ROOT_NOT_CURRENT in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    _assert_non_authorizing(report)


def test_configured_root_is_usable_at_exact_validity_start() -> None:
    private, public = _key_material()
    root = ConfiguredEd25519TrustAnchor(
        authority_id="FIXTURE.ISSUER",
        anchor_id="PHASE22.KEY.1",
        anchor_version="1",
        public_key_pem=public,
        valid_from=NOW,
        expires_at=NOW + timedelta(days=1),
        provenance_reference="synthetic-test-fixture",
    )
    configuration = _configuration(public, trust_anchor=root)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration)), evaluated_at=NOW
    )

    assert (
        report.disposition
        is EvidenceVerificationDisposition.CONFIGURED_KEY_VERIFIED_NON_AUTHORITATIVE
    )
    assert report.binding_status is BindingVerificationStatus.VALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert report.issuer_status is IssuerAuthenticationStatus.CONFIGURED_SYNTHETIC_KEY
    assert report.signature_status is SignatureVerificationStatus.VALID
    assert EvidenceVerificationReason.TRUST_ROOT_NOT_CURRENT not in report.reason_codes
    assert (
        EvidenceVerificationReason.SYNTHETIC_TRUST_ROOT_NON_AUTHORITATIVE
        in report.reason_codes
    )
    assert (
        EvidenceVerificationReason.INDEPENDENT_LIFECYCLE_VERIFICATION_UNAVAILABLE
        in report.reason_codes
    )
    assert (
        EvidenceVerificationReason.EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE
        in report.reason_codes
    )
    _assert_non_authorizing(report)


def test_configured_root_is_unusable_at_exact_expiry() -> None:
    private, public = _key_material()
    root = ConfiguredEd25519TrustAnchor(
        authority_id="FIXTURE.ISSUER",
        anchor_id="PHASE22.KEY.1",
        anchor_version="1",
        public_key_pem=public,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW,
        provenance_reference="synthetic-test-fixture",
    )
    configuration = _configuration(public, trust_anchor=root)
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration)), evaluated_at=NOW
    )

    assert report.disposition is EvidenceVerificationDisposition.STALE
    assert report.binding_status is BindingVerificationStatus.VALID
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert report.issuer_status is IssuerAuthenticationStatus.UNAVAILABLE
    assert EvidenceVerificationReason.TRUST_ROOT_NOT_CURRENT in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    _assert_non_authorizing(report)


def test_evidence_at_exact_maximum_age_is_fresh_but_non_authoritative() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    issued_at = NOW - timedelta(seconds=86_400)
    evidence = _evidence(
        issued_at=issued_at,
        verified_at=issued_at,
        expires_at=NOW + timedelta(minutes=1),
    )
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)),
        evaluated_at=NOW,
    )

    assert (
        report.disposition
        is EvidenceVerificationDisposition.CONFIGURED_KEY_VERIFIED_NON_AUTHORITATIVE
    )
    assert report.freshness_status is FreshnessVerificationStatus.FRESH
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert report.signature_status is SignatureVerificationStatus.VALID
    assert EvidenceVerificationReason.EVIDENCE_STALE not in report.reason_codes
    _assert_non_authorizing(report)


def test_evidence_just_beyond_maximum_age_is_stale() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    issued_at = NOW - timedelta(seconds=86_400, microseconds=1)
    evidence = _evidence(
        issued_at=issued_at,
        verified_at=issued_at,
        expires_at=NOW + timedelta(minutes=1),
    )
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)),
        evaluated_at=NOW,
    )

    assert report.disposition is EvidenceVerificationDisposition.STALE
    assert report.freshness_status is FreshnessVerificationStatus.STALE
    assert report.lifecycle_status is LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED
    assert EvidenceVerificationReason.EVIDENCE_STALE in report.reason_codes
    assert report.signature_status is SignatureVerificationStatus.NOT_CHECKED
    _assert_non_authorizing(report)


def test_caller_independent_verification_claim_is_ignored() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    evidence = _evidence(
        synthetic=False,
        provenance=EvidenceProvenance.INDEPENDENT_AUTHORITY,
        verification_result=EvidenceVerificationResult.INDEPENDENTLY_VERIFIED,
    )
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=_request(_signed(private, configuration, evidence=evidence)), evaluated_at=NOW
    )
    assert report.signature_status is SignatureVerificationStatus.VALID
    assert EvidenceVerificationReason.CALLER_VERIFICATION_CLAIM_IGNORED in report.reason_codes
    assert report.independently_verified is False


def test_duplicate_detection_is_pure_and_does_not_claim_durable_replay() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    signed = _signed(private, configuration)
    request = _request(
        signed,
        previously_seen_evidence_fingerprints=(signed.evidence.envelope_fingerprint,),
    )
    report = OfflineIndependentEvidenceVerifier(configuration).verify(
        request=request, evaluated_at=NOW
    )
    assert report.disposition is EvidenceVerificationDisposition.DUPLICATE
    assert report.replay_status is ReplayEvaluationStatus.DUPLICATE_IN_SUPPLIED_SET
    assert EvidenceVerificationReason.EVIDENCE_DUPLICATE_IN_SUPPLIED_SET in report.reason_codes
    assert EvidenceVerificationReason.EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE in report.reason_codes


def test_results_are_deterministic_bounded_and_immutable() -> None:
    private, public = _key_material()
    configuration = _configuration(public)
    request = _request(_signed(private, configuration))
    verifier = OfflineIndependentEvidenceVerifier(configuration)
    first = verifier.verify(request=request, evaluated_at=NOW)
    second = verifier.verify(request=request, evaluated_at=NOW)
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert all(type(code) is EvidenceVerificationReason for code in first.reason_codes)
    with pytest.raises(ValidationError):
        first.execution_authority = True


def test_model_rejects_blank_challenge_private_key_and_production_trust() -> None:
    with pytest.raises(ValidationError):
        _context(challenge_id="")
    private, public = _key_material()
    configuration = _configuration(public)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    with pytest.raises(ValidationError, match="private key"):
        _signed(private, configuration, presented_public_key_pem=private_pem)
    with pytest.raises(ValidationError, match="cannot permit production"):
        _configuration(
            public,
            permitted_environments=(IntegrationEnvironment.PRODUCTION,),
        )


def test_phase22_has_no_runtime_network_or_execution_wiring() -> None:
    service = Path("app/services/independent_evidence_verification.py").read_text(
        encoding="utf-8"
    )
    prohibited = (
        "requests",
        "httpx",
        "socket",
        "MetaTrader5",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "order_send",
    )
    assert all(item not in service for item in prohibited)
    phase22_modules = {
        "app.models.independent_evidence_verification",
        "app.services.independent_evidence_verification",
    }
    for path in Path("app").rglob("*.py"):
        if path.as_posix() in {
            "app/models/independent_evidence_verification.py",
            "app/services/independent_evidence_verification.py",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        assert all(module not in text for module in phase22_modules)
