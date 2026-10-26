"""Phase 19 end-to-end local external-trust integration tests."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.models.authenticated_external_trust import (
    AuthoritativeReconciliationLineage,
    ExternalReconciliationStatus,
    ExternalTrustAttestationRequest,
    ExternalTrustAttestationStatement,
    ExternalTrustCapability,
    ExternalTrustIntegrationStatus,
    ExternalTrustLifecycle,
    RollbackWitnessLineage,
    SignedExternalTrustAttestation,
    canonical_external_attestation_payload,
)
from app.models.external_trust_operations import (
    ExternalOperationsStatus,
    ExternalTransportContext,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.models.production_authorization import (
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.services.authenticated_external_trust import (
    AuthenticatedExternalTrustIntegration,
    build_broker_evidence_lineage,
)
from app.services.durable_independent_replay import DurableIndependentReplayStore
from app.services.external_trust_operations import ExternalTrustOperationalCoordinator
from app.services.production_authorization_readiness import ProductionAuthorizationReadinessGate
from tests.test_phase18_external_attestation import _evidence, _reconciliation


NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
PROVIDER = "SYNTHETIC-EXTERNAL-PROVIDER"
SCOPE = "a" * 64


def _key_and_root():
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private, ConfiguredEd25519TrustAnchor(
        authority_id="synthetic-external-authority",
        anchor_id="synthetic-external-root",
        anchor_version="v1",
        public_key_pem=public_pem,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        provenance_reference="test-only root pending independent provisioning",
    )


def _transport(*, configured=True):
    return ExternalTransportContext(
        provider_id=PROVIDER,
        endpoint_reference="disabled://external-provider-contract-only",
        connect_timeout_seconds=1,
        read_timeout_seconds=2,
        overall_timeout_seconds=2,
        maximum_concurrency=4,
        configured=configured,
        reason_codes=("TRANSPORT_AUTHENTICATION_NOT_OPERATIONAL",),
    )


def _witness(**changes):
    values = {
        "replay_store_id": "b" * 32,
        "replay_stream_fingerprint": "c" * 64,
        "sequence_head": 7,
        "history_checkpoint_fingerprint": "d" * 64,
        "witness_sequence": 8,
        "update_idempotency_key": "e" * 64,
        "update_outcome": "confirmed",
    }
    values.update(changes)
    return RollbackWitnessLineage(**values)


def _authoritative_reconciliation(evidence, **changes):
    broker = build_broker_evidence_lineage(evidence, _reconciliation())
    values = {
        "account_fingerprint": broker.account_fingerprint,
        "scope_fingerprint": evidence.request.fingerprint,
        "evidence_request_fingerprint": evidence.request.fingerprint,
        "evidence_fingerprint": evidence.fingerprint,
        "snapshot_generation": 1,
        "reservation_fingerprint": "f" * 64,
        "execution_history_fingerprint": "1" * 64,
        "replay_checkpoint_fingerprint": "2" * 64,
        "reconciliation_generation": 1,
        "coverage_complete": True,
        "record_count": 0,
        "status": "matched",
    }
    values.update(changes)
    return AuthoritativeReconciliationLineage(**values)


def _request(capability, root, evidence, witness, reconciliation, index):
    values = {
        "request_id": f"phase19-request-{index:032d}",
        "challenge_id": f"phase19-challenge-{index:030d}",
        "expected_provider_id": PROVIDER,
        "capability": capability,
        "subject_id": capability.value,
        "subject_generation": 1,
        "subject_fingerprint": root.public_key_fingerprint,
        "scope_fingerprint": SCOPE,
        "policy_version": "v1",
        "minimum_source_sequence": 0,
        "requested_at": NOW - timedelta(minutes=2),
        "max_age_seconds": 300,
    }
    if capability is ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION:
        lineage = build_broker_evidence_lineage(evidence, _reconciliation())
        values.update(
            subject_fingerprint=lineage.evidence_fingerprint,
            scope_fingerprint=lineage.evidence_request_fingerprint,
            broker_lineage=lineage,
        )
    elif capability is ExternalTrustCapability.ROLLBACK_WITNESS:
        values.update(
            subject_fingerprint=witness.checkpoint_fingerprint,
            scope_fingerprint=witness.replay_stream_fingerprint,
            witness_lineage=witness,
        )
    elif capability is ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION:
        values.update(
            subject_fingerprint=reconciliation.reconciliation_fingerprint,
            scope_fingerprint=reconciliation.scope_fingerprint,
            reconciliation_lineage=reconciliation,
        )
    return ExternalTrustAttestationRequest(**values)


def _statement(request, root, sequence):
    values = {
        "statement_id": f"phase19-statement-{sequence:030d}",
        "provider_id": PROVIDER,
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
        "source_sequence": sequence,
        "issued_at": NOW - timedelta(minutes=1),
        "observed_at": NOW - timedelta(seconds=45),
        "retrieved_at": NOW - timedelta(seconds=30),
        "expires_at": NOW + timedelta(minutes=4),
        "lifecycle": ExternalTrustLifecycle.ACTIVE,
    }
    if request.capability is ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION:
        values.update(
            lifecycle=None,
            evidence_fingerprint=request.broker_lineage.evidence_fingerprint,
            broker_lineage=request.broker_lineage,
            reconciliation_status=ExternalReconciliationStatus.COMPLETE,
        )
    elif request.capability is ExternalTrustCapability.ROLLBACK_WITNESS:
        values.update(
            lifecycle=None,
            source_sequence=request.witness_lineage.witness_sequence,
            witness_lineage=request.witness_lineage,
        )
    elif request.capability is ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION:
        matched = request.reconciliation_lineage.status == "matched"
        values.update(
            lifecycle=None,
            source_sequence=request.reconciliation_lineage.reconciliation_generation,
            evidence_fingerprint=request.reconciliation_lineage.evidence_fingerprint,
            reconciliation_lineage=request.reconciliation_lineage,
            reconciliation_status=(
                ExternalReconciliationStatus.COMPLETE
                if matched
                else ExternalReconciliationStatus.UNRESOLVED
            ),
            reason_codes=() if matched else ("EXTERNAL_RECONCILIATION_INCOMPLETE",),
        )
    return ExternalTrustAttestationStatement(**values)


def _signed(statement, private):
    return SignedExternalTrustAttestation(
        statement=statement,
        signature_base64=base64.b64encode(
            private.sign(canonical_external_attestation_payload(statement))
        ).decode("ascii"),
    )


class _Provider:
    provider_id = PROVIDER

    def __init__(self, envelope):
        self._envelope = envelope

    def read_attestation(self, _request):
        return self._envelope


def _assessments(path: Path):
    private, root = _key_and_root()
    evidence = _evidence(evaluated_at=NOW)
    witness = _witness()
    reconciliation = _authoritative_reconciliation(evidence)
    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    results = {}
    for index, capability in enumerate(ExternalTrustCapability, start=1):
        request = _request(capability, root, evidence, witness, reconciliation, index)
        statement = _statement(request, root, index)
        results[capability] = AuthenticatedExternalTrustIntegration(
            _Provider(_signed(statement, private)),
            verification_root=root,
            replay_store=store,
        ).assess(request, evaluated_at=NOW)
    return results, evidence, witness, reconciliation


def _compose(path: Path, **changes):
    results, evidence, witness, reconciliation = _assessments(path)
    values = {
        "evaluated_at": NOW,
        "transport": _transport(),
        "root_provisioning": results[ExternalTrustCapability.ROOT_PROVISIONING],
        "lifecycle": results[ExternalTrustCapability.LIFECYCLE_STATUS],
        "witness": results[ExternalTrustCapability.ROLLBACK_WITNESS],
        "broker": results[ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION],
        "reconciliation": results[ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION],
        "evidence": evidence,
        "broker_reconciliation": _reconciliation(),
        "expected_witness": witness,
        "expected_reconciliation": reconciliation,
    }
    values.update(changes)
    return ExternalTrustOperationalCoordinator().assess(**values)


def test_complete_synthetic_protocol_remains_blocked_and_non_authorizing(tmp_path) -> None:
    result = _compose(tmp_path / "replay.sqlite3")
    assert result.status is ExternalOperationsStatus.STRUCTURALLY_VALIDATED_UNAUTHENTICATED
    assert result.local_bindings_verified is True
    assert result.replay_consumed is True
    assert result.independent_root_provisioned is False
    assert result.authoritative_lifecycle_available is False
    assert result.transport_authenticated is False
    assert result.external_rollback_assurance is False
    assert result.broker_evidence_authenticated is False
    assert result.reconciliation_authoritative is False
    assert result.production_authorization_available is False
    assert result.submission_allowed is False
    assert result.execution_authority is False

    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW, external_operations=result
    )
    check = {item.prerequisite: item for item in report.checks}[
        ProductionReadinessPrerequisite.EXTERNAL_TRUST_OPERATIONS
    ]
    assert check.status is ProductionReadinessCheckStatus.BLOCKED
    assert report.production_authorization_available is False


def test_unconfigured_transport_and_missing_components_fail_closed(tmp_path) -> None:
    result = ExternalTrustOperationalCoordinator().assess(
        evaluated_at=NOW,
        transport=_transport(configured=False),
        root_provisioning=None,
        lifecycle=None,
        witness=None,
        broker=None,
        reconciliation=None,
        evidence=None,
        broker_reconciliation=None,
        expected_witness=None,
        expected_reconciliation=None,
    )
    assert result.status is ExternalOperationsStatus.UNCONFIGURED
    assert result.local_bindings_verified is False
    assert result.execution_authority is False


def test_transport_policy_is_bounded_sanitized_and_non_authoritative() -> None:
    transport = _transport()
    with pytest.raises(ValidationError):
        type(transport).model_validate(
            {**transport.model_dump(), "transport_authenticated": True}
        )
    with pytest.raises(ValidationError):
        ExternalTransportContext(
            provider_id=PROVIDER,
            endpoint_reference="https://provider.invalid?token=synthetic-secret",
            connect_timeout_seconds=1,
            read_timeout_seconds=2,
            overall_timeout_seconds=2,
            maximum_concurrency=4,
            configured=True,
            reason_codes=("TRANSPORT_AUTHENTICATION_NOT_OPERATIONAL",),
        )
    with pytest.raises(ValidationError):
        ExternalTransportContext(
            provider_id=PROVIDER,
            endpoint_reference="disabled://external-provider-contract-only",
            connect_timeout_seconds=2,
            read_timeout_seconds=3,
            overall_timeout_seconds=1,
            maximum_concurrency=4,
            configured=True,
            reason_codes=("TRANSPORT_AUTHENTICATION_NOT_OPERATIONAL",),
        )


@pytest.mark.parametrize(
    "change",
    (
        {"expected_witness": _witness(sequence_head=6)},
        {"expected_witness": _witness(update_outcome="unknown")},
    ),
)
def test_witness_rollback_or_uncertain_update_fails_closed(tmp_path, change) -> None:
    result = _compose(tmp_path / "replay.sqlite3", **change)
    assert result.status is ExternalOperationsStatus.CONFLICT
    assert result.external_rollback_assurance is False


def test_reconciliation_substitution_and_incomplete_coverage_fail_closed(tmp_path) -> None:
    results, evidence, witness, reconciliation = _assessments(tmp_path / "replay.sqlite3")
    wrong = reconciliation.model_copy(update={"reservation_fingerprint": "3" * 64})
    result = ExternalTrustOperationalCoordinator().assess(
        evaluated_at=NOW,
        transport=_transport(),
        root_provisioning=results[ExternalTrustCapability.ROOT_PROVISIONING],
        lifecycle=results[ExternalTrustCapability.LIFECYCLE_STATUS],
        witness=results[ExternalTrustCapability.ROLLBACK_WITNESS],
        broker=results[ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION],
        reconciliation=results[ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION],
        evidence=evidence,
        broker_reconciliation=_reconciliation(),
        expected_witness=witness,
        expected_reconciliation=wrong,
    )
    assert result.status is ExternalOperationsStatus.CONFLICT
    assert result.reconciliation_authoritative is False
    with pytest.raises(ValidationError):
        _authoritative_reconciliation(
            evidence, status="unresolved", coverage_complete=True
        )


def test_signed_witness_unknown_outcome_is_consumed_then_blocked(tmp_path) -> None:
    private, root = _key_and_root()
    evidence = _evidence(evaluated_at=NOW)
    witness = _witness(update_outcome="unknown")
    reconciliation = _authoritative_reconciliation(evidence)
    request = _request(
        ExternalTrustCapability.ROLLBACK_WITNESS,
        root,
        evidence,
        witness,
        reconciliation,
        9,
    )
    statement = _statement(request, root, 9)
    result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(statement, private)),
        verification_root=root,
        replay_store=DurableIndependentReplayStore.initialize(
            tmp_path / "replay.sqlite3", now=NOW
        ),
    ).assess(request, evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.CONFLICT
    assert result.replay_consumed is True
    assert result.external_rollback_assurance is False


def test_witness_sequence_rollback_is_rejected_across_store_restart(tmp_path) -> None:
    private, root = _key_and_root()
    evidence = _evidence(evaluated_at=NOW)
    reconciliation = _authoritative_reconciliation(evidence)
    path = tmp_path / "replay.sqlite3"
    first_witness = _witness(witness_sequence=8)
    first_request = _request(
        ExternalTrustCapability.ROLLBACK_WITNESS,
        root,
        evidence,
        first_witness,
        reconciliation,
        10,
    )
    first = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(_statement(first_request, root, 8), private)),
        verification_root=root,
        replay_store=DurableIndependentReplayStore.initialize(path, now=NOW),
    ).assess(first_request, evaluated_at=NOW)
    assert first.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED

    lower_witness = _witness(witness_sequence=7, sequence_head=6)
    lower_request = _request(
        ExternalTrustCapability.ROLLBACK_WITNESS,
        root,
        evidence,
        lower_witness,
        reconciliation,
        11,
    )
    lower = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(_statement(lower_request, root, 7), private)),
        verification_root=root,
        replay_store=DurableIndependentReplayStore(path),
    ).assess(lower_request, evaluated_at=NOW)
    assert lower.status is ExternalTrustIntegrationStatus.INVALID
    assert "AUTHORITY_REPLAY_SEQUENCE_ROLLBACK" in lower.reason_codes
    assert lower.external_rollback_assurance is False


@pytest.mark.parametrize("status", ("missing", "duplicate", "conflict", "unresolved"))
def test_signed_incomplete_reconciliation_is_consumed_then_blocked(
    tmp_path, status
) -> None:
    private, root = _key_and_root()
    evidence = _evidence(evaluated_at=NOW)
    witness = _witness()
    reconciliation = _authoritative_reconciliation(
        evidence,
        status=status,
        coverage_complete=False,
    )
    request = _request(
        ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION,
        root,
        evidence,
        witness,
        reconciliation,
        12,
    )
    statement = _statement(request, root, reconciliation.reconciliation_generation)
    result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(statement, private)),
        verification_root=root,
        replay_store=DurableIndependentReplayStore.initialize(
            tmp_path / "replay.sqlite3", now=NOW
        ),
    ).assess(request, evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.CONFLICT
    assert result.replay_consumed is True
    assert result.reconciliation_authoritative is False


def test_root_substitution_and_self_signed_root_do_not_establish_independence(tmp_path) -> None:
    results, _evidence_value, _witness_value, _reconciliation_value = _assessments(
        tmp_path / "replay.sqlite3"
    )
    root = results[ExternalTrustCapability.ROOT_PROVISIONING]
    assert root.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED
    assert root.independently_authenticated is False
    payload = root.model_dump()
    with pytest.raises(ValidationError):
        type(root).model_validate({**payload, "independently_authenticated": True})


def test_operational_result_cannot_be_forged_into_authority(tmp_path) -> None:
    result = _compose(tmp_path / "replay.sqlite3")
    payload = result.model_dump()
    for field in (
        "independent_root_provisioned",
        "authoritative_lifecycle_available",
        "transport_authenticated",
        "external_rollback_assurance",
        "broker_evidence_authenticated",
        "reconciliation_authoritative",
        "production_authorization_available",
        "submission_allowed",
        "execution_authority",
    ):
        with pytest.raises(ValidationError):
            type(result).model_validate({**payload, field: True})


def test_phase19_boundary_has_no_execution_or_network_reachability() -> None:
    root = Path(__file__).parents[1] / "app"
    text = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "models/external_trust_operations.py",
            "services/external_trust_operations.py",
        )
    )
    for forbidden in (
        "MetaTrader5",
        "order_send",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        ".submit(",
        "requests.",
        "httpx.",
        "socket.",
    ):
        assert forbidden not in text
