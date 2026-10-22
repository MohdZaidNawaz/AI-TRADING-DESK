from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.models.independent_trust_governance import (
    ExternalRollbackWitnessObservation,
    ExternalRollbackWitnessRequest,
    ExternalRollbackWitnessStatus,
    IndependentTrustGovernanceStatus,
    SignedTrustAnchorGovernanceManifest,
    TrustAnchorGovernanceManifest,
    TrustAnchorGovernanceRequest,
    TrustAnchorLifecycle,
    canonical_governance_manifest_payload,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.models.personal_trust_bootstrap import PersonalTrustScope
from app.models.production_authorization import (
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.services.durable_independent_replay import DurableIndependentReplayStore
from app.services.independent_trust_governance import (
    ExternalRollbackWitnessIntegration,
    IndependentTrustGovernanceVerifier,
)
from app.services.production_authorization_readiness import ProductionAuthorizationReadinessGate


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def _key_and_root():
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    root = ConfiguredEd25519TrustAnchor(
        authority_id="governance-root-authority",
        anchor_id="governance-root",
        anchor_version="v1",
        public_key_pem=public_pem,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        provenance_reference="operator configured root pending independent authentication",
    )
    return private, root


def _scope() -> PersonalTrustScope:
    return PersonalTrustScope(
        scope_id="xauusd-demo-scope",
        account_id="demo-account",
        execution_mode="disabled",
        canonical_symbol="XAUUSD",
        risk_currency="USD",
        policy_id="controlled-demo-policy",
        policy_version="v1",
    )


def _request(**changes: object) -> TrustAnchorGovernanceRequest:
    values: dict[str, object] = {
        "request_id": "governance-request-00000000000001",
        "challenge_id": "governance-challenge-000000000001",
        "authority_id": "personal-key-authority",
        "anchor_id": "personal-key-status-anchor",
        "anchor_version": "v1",
        "anchor_generation": 1,
        "anchor_fingerprint": "a" * 64,
        "scope": _scope(),
        "policy_version": "v1",
        "minimum_source_sequence": 0,
        "requested_at": NOW - timedelta(minutes=2),
        "max_age_seconds": 300,
    }
    values.update(changes)
    return TrustAnchorGovernanceRequest(**values)


def _manifest(
    request: TrustAnchorGovernanceRequest,
    root: ConfiguredEd25519TrustAnchor,
    **changes: object,
) -> TrustAnchorGovernanceManifest:
    values: dict[str, object] = {
        "manifest_id": "governance-manifest-0000000000001",
        "governance_authority_id": root.authority_id,
        "governance_root_id": root.anchor_id,
        "governance_root_version": root.anchor_version,
        "governance_root_fingerprint": root.public_key_fingerprint,
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "authority_id": request.authority_id,
        "anchor_id": request.anchor_id,
        "anchor_version": request.anchor_version,
        "anchor_generation": request.anchor_generation,
        "anchor_fingerprint": request.anchor_fingerprint,
        "scope": request.scope,
        "scope_fingerprint": request.scope.fingerprint,
        "policy_version": request.policy_version,
        "lifecycle": TrustAnchorLifecycle.ACTIVE,
        "source_sequence": 1,
        "issued_at": NOW - timedelta(minutes=1),
        "valid_from": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return TrustAnchorGovernanceManifest(**values)


def _signed(
    manifest: TrustAnchorGovernanceManifest,
    private: Ed25519PrivateKey,
) -> SignedTrustAnchorGovernanceManifest:
    return SignedTrustAnchorGovernanceManifest(
        manifest=manifest,
        signature_base64=base64.b64encode(
            private.sign(canonical_governance_manifest_payload(manifest))
        ).decode("ascii"),
    )


def _witness_request(**changes: object) -> ExternalRollbackWitnessRequest:
    values: dict[str, object] = {
        "request_id": "rollback-witness-request-000000001",
        "challenge_id": "rollback-witness-challenge-00001",
        "expected_provider_id": "external-witness",
        "authority_id": "personal-key-authority",
        "anchor_generation": 1,
        "anchor_fingerprint": "a" * 64,
        "replay_store_id": "b" * 32,
        "replay_checkpoint_fingerprint": "c" * 64,
        "requested_at": NOW - timedelta(minutes=2),
        "max_age_seconds": 300,
    }
    values.update(changes)
    return ExternalRollbackWitnessRequest(**values)


def _witness_observation(
    request: ExternalRollbackWitnessRequest,
    **changes: object,
) -> ExternalRollbackWitnessObservation:
    values: dict[str, object] = {
        "available": True,
        "provider_id": request.expected_provider_id,
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "authority_id": request.authority_id,
        "anchor_generation": request.anchor_generation,
        "anchor_fingerprint": request.anchor_fingerprint,
        "replay_store_id": request.replay_store_id,
        "replay_checkpoint_fingerprint": request.replay_checkpoint_fingerprint,
        "witness_sequence": 7,
        "observed_at": NOW - timedelta(minutes=1),
        "retrieved_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(changes)
    return ExternalRollbackWitnessObservation(**values)


class _WitnessProvider:
    provider_id = "EXTERNAL-WITNESS"

    def __init__(self, observation: ExternalRollbackWitnessObservation) -> None:
        self._observation = observation

    def observe(self, _request: ExternalRollbackWitnessRequest) -> ExternalRollbackWitnessObservation:
        return self._observation


def _checks(report):
    return {check.prerequisite: check for check in report.checks}


def test_unconfigured_governance_and_default_witness_fail_closed() -> None:
    request = _request()
    governance = IndependentTrustGovernanceVerifier().assess(
        request=request,
        envelope=None,
        evaluated_at=NOW,
    )
    witness_request = _witness_request()
    witness = ExternalRollbackWitnessIntegration().assess(
        request=witness_request,
        evaluated_at=NOW,
    )

    assert governance.status is IndependentTrustGovernanceStatus.UNCONFIGURED
    assert governance.independent_authentication_established is False
    assert witness.status is ExternalRollbackWitnessStatus.UNAVAILABLE
    assert witness.external_rollback_assurance is False
    assert governance.production_authorization_available is witness.production_authorization_available is False


def test_valid_governance_signature_remains_configured_but_unauthenticated() -> None:
    private, root = _key_and_root()
    request = _request()
    envelope = _signed(_manifest(request, root), private)

    result = IndependentTrustGovernanceVerifier(root).assess(
        request=request,
        envelope=envelope,
        evaluated_at=NOW,
    )

    assert result.status is IndependentTrustGovernanceStatus.CONFIGURED_UNAUTHENTICATED
    assert result.signature_verified is True
    assert result.lineage_verified is True
    assert result.reason_codes == ("GOVERNANCE_ROOT_NOT_INDEPENDENTLY_AUTHENTICATED",)
    assert result.production_authorization_available is False
    assert result.submission_allowed is False
    assert result.execution_authority is False


def test_evidence_supplied_or_substituted_key_cannot_establish_trust() -> None:
    private, root = _key_and_root()
    attacker, attacker_root = _key_and_root()
    request = _request()
    attacker_manifest = _manifest(
        request,
        attacker_root,
        governance_authority_id=root.authority_id,
        governance_root_id=root.anchor_id,
        governance_root_version=root.anchor_version,
        governance_root_fingerprint=root.public_key_fingerprint,
    )
    result = IndependentTrustGovernanceVerifier(root).assess(
        request=request,
        envelope=_signed(attacker_manifest, attacker),
        evaluated_at=NOW,
    )

    assert result.status is IndependentTrustGovernanceStatus.INVALID
    assert "GOVERNANCE_MANIFEST_SIGNATURE_INVALID" in result.reason_codes
    assert result.signature_verified is False
    assert private is not attacker


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"expires_at": NOW}, IndependentTrustGovernanceStatus.EXPIRED),
        ({"anchor_fingerprint": "d" * 64}, IndependentTrustGovernanceStatus.INVALID),
        ({"source_sequence": 1}, IndependentTrustGovernanceStatus.INVALID),
    ],
)
def test_expired_mismatched_and_downgraded_manifests_fail_closed(
    changes: dict[str, object], expected: IndependentTrustGovernanceStatus
) -> None:
    private, root = _key_and_root()
    request = _request(minimum_source_sequence=2 if "source_sequence" in changes else 0)
    result = IndependentTrustGovernanceVerifier(root).assess(
        request=request,
        envelope=_signed(_manifest(request, root, **changes), private),
        evaluated_at=NOW,
    )
    assert result.status is expected
    assert result.production_authorization_available is False


def test_revoked_anchor_is_terminal_even_with_valid_signature() -> None:
    private, root = _key_and_root()
    request = _request()
    envelope = _signed(
        _manifest(
            request,
            root,
            lifecycle=TrustAnchorLifecycle.REVOKED,
            reason_code="ANCHOR_COMPROMISED",
        ),
        private,
    )
    result = IndependentTrustGovernanceVerifier(root).assess(
        request=request, envelope=envelope, evaluated_at=NOW
    )
    assert result.status is IndependentTrustGovernanceStatus.REVOKED
    assert result.reason_codes == ("ANCHOR_COMPROMISED",)
    assert result.independent_authentication_established is False


def test_rotation_requires_signed_explicit_previous_lineage() -> None:
    private, root = _key_and_root()
    current_request = _request(
        anchor_version="v2",
        anchor_generation=2,
        anchor_fingerprint="d" * 64,
        minimum_source_sequence=1,
    )
    missing = IndependentTrustGovernanceVerifier(root).assess(
        request=current_request,
        envelope=_signed(
            _manifest(
                current_request,
                root,
                source_sequence=2,
                previous_manifest_fingerprint="e" * 64,
            ),
            private,
        ),
        evaluated_at=NOW,
    )
    assert missing.status is IndependentTrustGovernanceStatus.INVALID
    assert "GOVERNANCE_ROTATION_LINEAGE_MISSING" in missing.reason_codes

    prior_request = _request()
    prior = _manifest(
        prior_request,
        root,
        lifecycle=TrustAnchorLifecycle.ROTATION_PENDING,
        replacement_anchor_fingerprint=current_request.anchor_fingerprint,
        replacement_generation=2,
        reason_code="APPROVED_ROTATION_PENDING",
    )
    current = _manifest(
        current_request,
        root,
        source_sequence=2,
        previous_manifest_fingerprint=prior.manifest_fingerprint,
    )
    result = IndependentTrustGovernanceVerifier(root).assess(
        request=current_request,
        envelope=_signed(current, private),
        previous_envelope=_signed(prior, private),
        evaluated_at=NOW,
    )
    assert result.status is IndependentTrustGovernanceStatus.CONFIGURED_UNAUTHENTICATED

    unauthorized = prior.model_copy(
        update={
            "lifecycle": TrustAnchorLifecycle.ACTIVE,
            "replacement_anchor_fingerprint": None,
            "replacement_generation": None,
            "reason_code": None,
        }
    )
    rejected = IndependentTrustGovernanceVerifier(root).assess(
        request=current_request,
        envelope=_signed(current, private),
        previous_envelope=_signed(unauthorized, private),
        evaluated_at=NOW,
    )
    assert "GOVERNANCE_ROTATION_NOT_AUTHORIZED" in rejected.reason_codes


def test_unknown_manifest_version_and_fabricated_independent_status_are_rejected() -> None:
    private, root = _key_and_root()
    request = _request()
    payload = _manifest(request, root).model_dump(exclude={"manifest_fingerprint"})
    payload["schema_version"] = "trust-anchor-governance-manifest/v0"
    with pytest.raises(ValidationError):
        TrustAnchorGovernanceManifest.model_validate(payload)

    result = IndependentTrustGovernanceVerifier(root).assess(
        request=request,
        envelope=_signed(_manifest(request, root), private),
        evaluated_at=NOW,
    )
    payload = result.model_dump(exclude={"fingerprint"})
    payload["status"] = IndependentTrustGovernanceStatus.VALID_INDEPENDENTLY_AUTHENTICATED
    with pytest.raises(ValidationError, match="cannot construct"):
        type(result).model_validate(payload)


def test_external_witness_structural_match_never_claims_authentication_or_assurance() -> None:
    request = _witness_request()
    result = ExternalRollbackWitnessIntegration(
        _WitnessProvider(_witness_observation(request))
    ).assess(request=request, evaluated_at=NOW)

    assert result.status is ExternalRollbackWitnessStatus.STRUCTURALLY_VALIDATED
    assert result.reason_codes == ("EXTERNAL_WITNESS_AUTHENTICATION_NOT_CONFIGURED",)
    assert result.provider_authenticated is False
    assert result.independent_monotonicity_established is False
    assert result.external_rollback_assurance is False

    payload = _witness_observation(request).model_dump(exclude={"observation_fingerprint"})
    payload["provider_authenticated"] = True
    with pytest.raises(ValidationError):
        ExternalRollbackWitnessObservation.model_validate(payload)


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"anchor_generation": 2}, "EXTERNAL_ROLLBACK_WITNESS_TRUST_GENERATION_MISMATCH"),
        ({"replay_checkpoint_fingerprint": "d" * 64}, "EXTERNAL_ROLLBACK_WITNESS_CHECKPOINT_MISMATCH"),
        ({"observed_at": NOW - timedelta(minutes=10)}, "EXTERNAL_ROLLBACK_WITNESS_STALE"),
    ],
)
def test_external_witness_wrong_generation_checkpoint_and_stale_state_fail_closed(
    changes: dict[str, object], expected_code: str
) -> None:
    request = _witness_request()
    result = ExternalRollbackWitnessIntegration(
        _WitnessProvider(_witness_observation(request, **changes))
    ).assess(request=request, evaluated_at=NOW)
    assert expected_code in result.reason_codes
    assert result.external_rollback_assurance is False


def test_readiness_keeps_governance_and_external_witness_blocked(
    tmp_path: Path,
) -> None:
    private, root = _key_and_root()
    request = _request()
    governance = IndependentTrustGovernanceVerifier(root).assess(
        request=request,
        envelope=_signed(_manifest(request, root), private),
        evaluated_at=NOW,
    )
    witness_request = _witness_request()
    witness = ExternalRollbackWitnessIntegration(
        _WitnessProvider(_witness_observation(witness_request))
    ).assess(request=witness_request, evaluated_at=NOW)
    replay = DurableIndependentReplayStore.initialize(tmp_path / "replay.sqlite3", now=NOW)
    report = ProductionAuthorizationReadinessGate(replay_store=replay).evaluate(
        now=NOW,
        trust_governance=governance,
        rollback_witness=witness,
    )
    checks = _checks(report)

    assert checks[ProductionReadinessPrerequisite.LOCAL_KEY_CUSTODY].status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert checks[ProductionReadinessPrerequisite.INDEPENDENT_TRUST_ANCHOR].status is ProductionReadinessCheckStatus.BLOCKED
    assert checks[ProductionReadinessPrerequisite.INDEPENDENT_ANCHOR_LIFECYCLE].status is ProductionReadinessCheckStatus.BLOCKED
    assert checks[ProductionReadinessPrerequisite.EXTERNAL_ROLLBACK_ASSURANCE].status is ProductionReadinessCheckStatus.BLOCKED
    assert checks[ProductionReadinessPrerequisite.INDEPENDENT_REPLAY_STORAGE].status is ProductionReadinessCheckStatus.SATISFIED
    assert checks[ProductionReadinessPrerequisite.ATOMIC_REPLAY_CONSUMPTION].status is ProductionReadinessCheckStatus.SATISFIED
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_phase15_modules_have_no_execution_or_network_reachability() -> None:
    sources = (
        Path("app/models/independent_trust_governance.py").read_text(encoding="utf-8"),
        Path("app/services/independent_trust_governance.py").read_text(encoding="utf-8"),
    )
    for source in sources:
        for forbidden in (
            "MetaTrader5",
            "order_send",
            "MT5ExecutionAdapter",
            "PilotExecutionCoordinator",
            "FinalExecutionGate",
            ".submit(",
            "requests.",
            "httpx.",
        ):
            assert forbidden not in source
