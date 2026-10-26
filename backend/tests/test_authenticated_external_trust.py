from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.models.authenticated_external_trust import (
    ExternalReconciliationStatus,
    ExternalTrustAttestationRequest,
    ExternalTrustAttestationStatement,
    ExternalTrustCapability,
    ExternalTrustIntegrationStatus,
    ExternalTrustLifecycle,
    SignedExternalTrustAttestation,
    canonical_external_attestation_payload,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.models.production_authorization import (
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.services.authenticated_external_trust import AuthenticatedExternalTrustIntegration
from app.services.production_authorization_readiness import ProductionAuthorizationReadinessGate


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _key_and_root():
    private = Ed25519PrivateKey.generate()
    pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return private, ConfiguredEd25519TrustAnchor(
        authority_id="external-verification-authority",
        anchor_id="external-verification-root",
        anchor_version="v1",
        public_key_pem=pem,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        provenance_reference="locally configured pending independent authentication",
    )


def _request(capability=ExternalTrustCapability.ROOT_PROVISIONING, **changes):
    values = {
        "request_id": "external-trust-request-000000000001",
        "challenge_id": "external-trust-challenge-00000001",
        "expected_provider_id": "independent-external-provider",
        "capability": capability,
        "subject_id": "governance-root",
        "subject_generation": 1,
        "subject_fingerprint": "a" * 64,
        "scope_fingerprint": "b" * 64,
        "policy_version": "v1",
        "minimum_source_sequence": 1,
        "requested_at": NOW - timedelta(minutes=2),
        "max_age_seconds": 300,
    }
    values.update(changes)
    return ExternalTrustAttestationRequest(**values)


def _statement(request, root, **changes):
    values = {
        "statement_id": "external-trust-statement-0000000001",
        "provider_id": request.expected_provider_id,
        "verification_authority_id": root.authority_id,
        "verification_root_id": root.anchor_id,
        "verification_root_version": root.anchor_version,
        "verification_root_fingerprint": root.public_key_fingerprint,
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "capability": request.capability,
        "subject_id": request.subject_id,
        "subject_generation": request.subject_generation,
        "subject_fingerprint": request.subject_fingerprint,
        "scope_fingerprint": request.scope_fingerprint,
        "policy_version": request.policy_version,
        "source_sequence": 2,
        "issued_at": NOW - timedelta(minutes=1),
        "observed_at": NOW - timedelta(seconds=45),
        "retrieved_at": NOW - timedelta(seconds=30),
        "expires_at": NOW + timedelta(minutes=4),
        "lifecycle": ExternalTrustLifecycle.ACTIVE,
    }
    if request.capability is ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION:
        values.update(
            lifecycle=None,
            evidence_fingerprint="c" * 64,
            reconciliation_status=ExternalReconciliationStatus.COMPLETE,
        )
    values.update(changes)
    return ExternalTrustAttestationStatement(**values)


def _signed(statement, private):
    return SignedExternalTrustAttestation(
        statement=statement,
        signature_base64=base64.b64encode(
            private.sign(canonical_external_attestation_payload(statement))
        ).decode("ascii"),
    )


class _Provider:
    provider_id = "INDEPENDENT-EXTERNAL-PROVIDER"

    def __init__(self, envelope):
        self.envelope = envelope

    def read_attestation(self, request):
        del request
        return self.envelope


def test_unconfigured_default_is_unavailable_and_non_authorizing() -> None:
    result = AuthenticatedExternalTrustIntegration().assess(_request(), evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.UNCONFIGURED
    assert result.independently_authenticated is False
    assert result.production_authorization_available is False
    assert result.submission_allowed is False
    assert result.execution_authority is False


@pytest.mark.parametrize(
    "capability",
    list(ExternalTrustCapability),
)
def test_valid_signed_contract_remains_configured_unauthenticated(capability) -> None:
    private, root = _key_and_root()
    request = _request(capability)
    result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(_statement(request, root), private)), verification_root=root
    ).assess(request, evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED
    assert result.signature_verified and result.bindings_verified
    assert result.independently_authenticated is False
    assert result.authoritative_lifecycle is False
    assert result.broker_evidence_authenticated is False
    assert result.reconciliation_authoritative is False


def test_evidence_supplied_key_substitution_and_altered_payload_fail() -> None:
    trusted_private, trusted_root = _key_and_root()
    attacker_private, attacker_root = _key_and_root()
    request = _request()
    attacker_statement = _statement(
        request,
        attacker_root,
        verification_root_id=trusted_root.anchor_id,
        verification_root_version=trusted_root.anchor_version,
        verification_root_fingerprint=trusted_root.public_key_fingerprint,
    )
    substituted = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(attacker_statement, attacker_private)), verification_root=trusted_root
    ).assess(request, evaluated_at=NOW)
    assert substituted.status is ExternalTrustIntegrationStatus.INVALID
    assert "EXTERNAL_ATTESTATION_SIGNATURE_INVALID" in substituted.reason_codes

    valid = _signed(_statement(request, trusted_root), trusted_private)
    altered = valid.model_copy(
        update={"statement": valid.statement.model_copy(update={"source_sequence": 99})}
    )
    tampered = AuthenticatedExternalTrustIntegration(
        _Provider(altered), verification_root=trusted_root
    ).assess(request, evaluated_at=NOW)
    assert tampered.status is ExternalTrustIntegrationStatus.INVALID


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("challenge_id", "wrong-challenge-00000000000000001", "EXTERNAL_ATTESTATION_CHALLENGE_MISMATCH"),
        ("subject_fingerprint", "d" * 64, "EXTERNAL_ATTESTATION_FINGERPRINT_MISMATCH"),
        ("scope_fingerprint", "e" * 64, "EXTERNAL_ATTESTATION_SCOPE_MISMATCH"),
        ("source_sequence", 1, None),
    ],
)
def test_wrong_lineage_or_replay_fails_closed(field, value, code) -> None:
    private, root = _key_and_root()
    request = _request(minimum_source_sequence=2)
    statement = _statement(request, root, **{field: value})
    result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(statement, private)), verification_root=root
    ).assess(request, evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.INVALID
    assert code is None or code in result.reason_codes
    if field == "source_sequence":
        assert "EXTERNAL_ATTESTATION_SEQUENCE_ROLLBACK" in result.reason_codes


def test_stale_future_and_provider_failure_are_blocked() -> None:
    private, root = _key_and_root()
    request = _request()
    stale = _statement(
        request, root,
        issued_at=NOW - timedelta(minutes=10),
        observed_at=NOW - timedelta(minutes=9),
        retrieved_at=NOW - timedelta(minutes=8),
        expires_at=NOW + timedelta(minutes=1),
    )
    result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(stale, private)), verification_root=root
    ).assess(request, evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.INVALID or result.status is ExternalTrustIntegrationStatus.STALE
    assert "EXTERNAL_ATTESTATION_CHRONOLOGY_INVALID" in result.reason_codes or "EXTERNAL_ATTESTATION_STALE" in result.reason_codes

    class Broken:
        provider_id = request.expected_provider_id
        def read_attestation(self, _request):
            raise RuntimeError("sensitive backend detail")

    unavailable = AuthenticatedExternalTrustIntegration(Broken(), verification_root=root).assess(
        request, evaluated_at=NOW
    )
    assert unavailable.status is ExternalTrustIntegrationStatus.UNAVAILABLE
    assert all("sensitive" not in code.lower() for code in unavailable.reason_codes)


def test_revocation_and_unresolved_broker_reconciliation_fail_closed() -> None:
    private, root = _key_and_root()
    lifecycle = _request(ExternalTrustCapability.LIFECYCLE_STATUS)
    revoked = _statement(
        lifecycle, root, lifecycle=ExternalTrustLifecycle.REVOKED,
        reason_codes=("AUTHORITY_REVOKED",),
    )
    revoked_result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(revoked, private)), verification_root=root
    ).assess(lifecycle, evaluated_at=NOW)
    assert revoked_result.status is ExternalTrustIntegrationStatus.REVOKED
    assert revoked_result.authoritative_lifecycle is False

    broker = _request(ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION)
    conflict = _statement(
        broker, root,
        reconciliation_status=ExternalReconciliationStatus.UNRESOLVED,
        reason_codes=("BROKER_RECONCILIATION_CONFLICT",),
    )
    conflict_result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(conflict, private)), verification_root=root
    ).assess(broker, evaluated_at=NOW)
    assert conflict_result.status is ExternalTrustIntegrationStatus.CONFLICT
    assert conflict_result.broker_evidence_authenticated is False
    assert conflict_result.reconciliation_authoritative is False


def test_models_reject_ambiguous_chronology_and_fabricated_authority() -> None:
    _, root = _key_and_root()
    request = _request()
    with pytest.raises(ValidationError):
        _statement(request, root, retrieved_at=NOW - timedelta(minutes=2))
    result = AuthenticatedExternalTrustIntegration().assess(request, evaluated_at=NOW)
    payload = result.model_dump()
    for field in (
        "independently_authenticated", "authoritative_lifecycle",
        "external_rollback_assurance", "broker_evidence_authenticated",
        "reconciliation_authoritative", "production_authorization_available",
        "submission_allowed", "execution_authority",
    ):
        with pytest.raises(ValidationError):
            type(result).model_validate({**payload, field: True})


def test_readiness_reports_distinct_external_blockers_and_remains_false() -> None:
    private, root = _key_and_root()
    root_request = _request()
    root_assessment = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(_statement(root_request, root), private)),
        verification_root=root,
    ).assess(root_request, evaluated_at=NOW)
    broker_request = _request(ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION)
    broker_assessment = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(_statement(broker_request, root), private)),
        verification_root=root,
    ).assess(broker_request, evaluated_at=NOW)

    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW,
        root_provisioning=root_assessment,
        broker_authentication=broker_assessment,
    )
    checks = {check.prerequisite: check for check in report.checks}
    assert checks[ProductionReadinessPrerequisite.GOVERNANCE_ROOT_PROVISIONING].status is ProductionReadinessCheckStatus.BLOCKED
    assert checks[ProductionReadinessPrerequisite.ROTATION_RECOVERY_ASSURANCE].status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert checks[ProductionReadinessPrerequisite.AUTHENTICATED_BROKER_EVIDENCE].status is ProductionReadinessCheckStatus.BLOCKED
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_phase16_boundary_has_no_execution_or_network_reachability() -> None:
    root = Path(__file__).parents[1] / "app"
    text = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "models/authenticated_external_trust.py",
            "services/authenticated_external_trust.py",
        )
    )
    for forbidden in (
        "MetaTrader5", "order_send", "PilotExecutionCoordinator",
        "FinalExecutionGate", ".submit(", "requests.", "httpx.", "socket.",
    ):
        assert forbidden not in text
