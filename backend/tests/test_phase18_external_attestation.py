"""Phase 18 exact-lineage, durable-consumption, and timeout regressions."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from threading import Event

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.models.authenticated_external_trust import (
    ExternalReconciliationStatus,
    ExternalTrustAttestationRequest,
    ExternalTrustAttestationStatement,
    ExternalTrustCapability,
    ExternalTrustIntegrationStatus,
    SignedExternalTrustAttestation,
    canonical_external_attestation_payload,
)
from app.models.execution_state import ReconciliationReport, ReconciliationStatus
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.models.mt5 import BrokerInstrumentContract, MT5AccountSnapshot
from app.models.portfolio_risk import SourceAccountKey
from app.models.production_authorization import (
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.models.trusted_execution_evidence import (
    ExecutionEvidenceAuthenticationStatus,
    ExecutionEvidenceCompleteness,
    ExecutionEvidenceRequest,
    MarketReferenceObservation,
    PortfolioRiskInputObservation,
    ValidatedExecutionEvidence,
)
from app.services.authenticated_external_trust import (
    AuthenticatedExternalTrustIntegration,
    build_broker_evidence_lineage,
)
from app.services.durable_independent_replay import DurableIndependentReplayStore
from app.services.production_authorization_readiness import ProductionAuthorizationReadinessGate


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ACCOUNT = SourceAccountKey(
    source_id="controlled-test-source",
    source_account_id="controlled-test-account",
    account_reference="controlled-test-server",
)


def _root():
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
        provenance_reference="test-only configured root",
    )


def _evidence(*, evaluated_at: datetime = NOW) -> ValidatedExecutionEvidence:
    request = ExecutionEvidenceRequest(
        account=ACCOUNT,
        canonical_symbol="XAUUSD",
        broker_symbol="XAUUSD",
        max_age_seconds=300,
    )
    positions_fingerprint = sha256(b"[]").hexdigest()
    return ValidatedExecutionEvidence(
        request=request,
        provider_id="CONTROLLED_TEST_READ_ONLY_SOURCE",
        evaluated_at=evaluated_at,
        source_observed_at=NOW - timedelta(seconds=45),
        source_retrieved_at=NOW - timedelta(seconds=30),
        instrument=BrokerInstrumentContract(
            canonical_symbol="XAUUSD", broker_symbol="XAUUSD", digits=2,
            point="0.01", tick_size="0.01", minimum_volume="0.01",
            maximum_volume="100", volume_step="0.01", filling_modes=("FOK", "IOC"),
            order_modes=("MARKET",), tradable=True, quote_available=True,
        ),
        market=MarketReferenceObservation(
            canonical_symbol="XAUUSD", broker_symbol="XAUUSD", bid="100",
            ask="100.10", unit="USD", observed_at=NOW - timedelta(seconds=45),
        ),
        account=MT5AccountSnapshot(
            account=ACCOUNT, balance="1000", equity="1000", margin="0",
            free_margin="1000", currency="USD", trading_allowed=False,
            terminal_trade_allowed=False,
        ),
        portfolio_risk_inputs=PortfolioRiskInputObservation(
            account=ACCOUNT, position_snapshot_fingerprint=positions_fingerprint,
            policy_id="controlled-test-policy", policy_fingerprint="a" * 64,
            observed_at=NOW - timedelta(seconds=45),
        ),
        provider_authentication_status=ExecutionEvidenceAuthenticationStatus.UNAUTHENTICATED,
        snapshot_completeness=ExecutionEvidenceCompleteness.STRUCTURALLY_COMPLETE,
        ready=True,
    )


def _reconciliation(execution_id: str = "execution-controlled-test-0001") -> ReconciliationReport:
    return ReconciliationReport(
        execution_id=execution_id,
        status=ReconciliationStatus.MATCHED,
        record_fingerprint="b" * 64,
    )


def _request(evidence=None, reconciliation=None, **changes):
    evidence = evidence or _evidence()
    reconciliation = reconciliation or _reconciliation()
    values = {
        "request_id": "external-trust-request-phase18-000001",
        "challenge_id": "external-trust-challenge-phase18-001",
        "expected_provider_id": "independent-external-provider",
        "capability": ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION,
        "subject_id": "broker-snapshot",
        "subject_generation": 1,
        "subject_fingerprint": evidence.fingerprint,
        "scope_fingerprint": evidence.request.fingerprint,
        "policy_version": "v1",
        "minimum_source_sequence": 0,
        "requested_at": NOW - timedelta(minutes=2),
        "max_age_seconds": 300,
        "broker_lineage": build_broker_evidence_lineage(evidence, reconciliation),
    }
    values.update(changes)
    return ExternalTrustAttestationRequest(**values)


def _statement(request, root, *, sequence=1, statement_id="phase18-statement-000000000000001", **changes):
    values = {
        "statement_id": statement_id,
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
        "source_sequence": sequence,
        "issued_at": NOW - timedelta(minutes=1),
        "observed_at": NOW - timedelta(seconds=45),
        "retrieved_at": NOW - timedelta(seconds=30),
        "expires_at": NOW + timedelta(minutes=4),
        "evidence_fingerprint": request.broker_lineage.evidence_fingerprint,
        "broker_lineage": request.broker_lineage,
        "reconciliation_status": ExternalReconciliationStatus.COMPLETE,
    }
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


def _assess(path: Path, request, envelope, root):
    store = (
        DurableIndependentReplayStore(path)
        if path.exists()
        else DurableIndependentReplayStore.initialize(path, now=NOW)
    )
    return AuthenticatedExternalTrustIntegration(
        _Provider(envelope), verification_root=root, replay_store=store
    ).assess(request, evaluated_at=NOW)


def test_exact_broker_lineage_matches_but_readiness_remains_blocked(tmp_path) -> None:
    private, root = _root()
    evidence = _evidence()
    reconciliation = _reconciliation()
    request = _request(evidence, reconciliation)
    assessment = _assess(tmp_path / "replay.sqlite3", request, _signed(_statement(request, root), private), root)
    assert assessment.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED
    assert assessment.replay_consumed is True
    assert assessment.validated_statement.broker_lineage == request.broker_lineage

    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW, trusted_evidence=evidence, broker_authentication=assessment,
        broker_reconciliation=reconciliation,
    )
    check = {item.prerequisite: item for item in report.checks}[
        ProductionReadinessPrerequisite.AUTHENTICATED_BROKER_EVIDENCE
    ]
    assert check.status is ProductionReadinessCheckStatus.BLOCKED
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_assessment_for_evidence_a_cannot_be_reused_for_evidence_b(tmp_path) -> None:
    private, root = _root()
    evidence_a = _evidence()
    reconciliation = _reconciliation()
    request = _request(evidence_a, reconciliation)
    assessment = _assess(tmp_path / "replay.sqlite3", request, _signed(_statement(request, root), private), root)
    evidence_b = _evidence(evaluated_at=NOW + timedelta(seconds=1))
    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW, trusted_evidence=evidence_b, broker_authentication=assessment,
        broker_reconciliation=reconciliation,
    )
    check = {item.prerequisite: item for item in report.checks}[
        ProductionReadinessPrerequisite.AUTHENTICATED_BROKER_EVIDENCE
    ]
    assert check.status is ProductionReadinessCheckStatus.INVALID
    assert check.reason_codes == ("BROKER_ATTESTATION_EXACT_LINEAGE_MISMATCH",)


def test_signed_mismatched_broker_context_fails_before_consumption(tmp_path) -> None:
    private, root = _root()
    request = _request()
    wrong = request.broker_lineage.model_copy(update={"account_fingerprint": "f" * 64})
    statement = _statement(
        request,
        root,
        broker_lineage=wrong,
        evidence_fingerprint=wrong.evidence_fingerprint,
    )
    result = _assess(tmp_path / "replay.sqlite3", request, _signed(statement, private), root)
    assert result.status is ExternalTrustIntegrationStatus.INVALID
    assert "EXTERNAL_ATTESTATION_BROKER_LINEAGE_MISMATCH" in result.reason_codes
    assert result.replay_consumed is False


def test_every_signed_subject_and_snapshot_lineage_mismatch_fails_closed(tmp_path) -> None:
    private, root = _root()
    request = _request()
    lineage_changes = (
        {"account_fingerprint": "f" * 64},
        {"snapshot_retrieved_at": NOW - timedelta(seconds=29)},
        {"reconciliation_id": "different-reconciliation-0000001"},
        {"reconciliation_fingerprint": "e" * 64},
    )
    statement_changes = (
        {"request_fingerprint": "d" * 64},
        {"subject_id": "different-subject"},
        {"subject_generation": 2},
        {"policy_version": "v2"},
    )
    store = DurableIndependentReplayStore.initialize(tmp_path / "replay.sqlite3", now=NOW)
    for change in statement_changes:
        statement = _statement(request, root, **change)
        result = AuthenticatedExternalTrustIntegration(
            _Provider(_signed(statement, private)), verification_root=root, replay_store=store
        ).assess(request, evaluated_at=NOW)
        assert result.status is ExternalTrustIntegrationStatus.INVALID
        assert result.replay_consumed is False
    for change in lineage_changes:
        wrong = request.broker_lineage.model_copy(update=change)
        statement = _statement(
            request, root, broker_lineage=wrong,
            evidence_fingerprint=wrong.evidence_fingerprint,
        )
        result = AuthenticatedExternalTrustIntegration(
            _Provider(_signed(statement, private)), verification_root=root, replay_store=store
        ).assess(request, evaluated_at=NOW)
        assert result.status is ExternalTrustIntegrationStatus.INVALID
        assert "EXTERNAL_ATTESTATION_BROKER_LINEAGE_MISMATCH" in result.reason_codes
        assert result.replay_consumed is False


def test_duplicate_challenge_and_lower_sequence_fail_across_restart(tmp_path) -> None:
    private, root = _root()
    path = tmp_path / "replay.sqlite3"
    request = _request()
    envelope = _signed(_statement(request, root, sequence=2), private)
    first = _assess(path, request, envelope, root)
    duplicate = _assess(path, request, envelope, root)
    assert first.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED
    assert duplicate.status is ExternalTrustIntegrationStatus.INVALID
    assert "AUTHORITY_REPLAY_DUPLICATE" in duplicate.reason_codes

    lower_request = _request(
        challenge_id="external-trust-challenge-phase18-002",
        request_id="external-trust-request-phase18-000002",
    )
    lower = _assess(
        path,
        lower_request,
        _signed(_statement(lower_request, root, sequence=1, statement_id="phase18-statement-000000000000002"), private),
        root,
    )
    assert lower.status is ExternalTrustIntegrationStatus.INVALID
    assert "AUTHORITY_REPLAY_SEQUENCE_ROLLBACK" in lower.reason_codes


def test_concurrent_duplicate_has_exactly_one_accepted_consumption(tmp_path) -> None:
    private, root = _root()
    path = tmp_path / "replay.sqlite3"
    DurableIndependentReplayStore.initialize(path, now=NOW)
    request = _request()
    envelope = _signed(_statement(request, root), private)

    def consume(_index):
        return _assess(path, request, envelope, root).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = tuple(pool.map(consume, range(2)))
    assert statuses.count(ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED) == 1
    assert statuses.count(ExternalTrustIntegrationStatus.INVALID) == 1


def test_provider_sequence_streams_are_separated_by_exact_scope(tmp_path) -> None:
    private, root = _root()
    path = tmp_path / "replay.sqlite3"
    evidence_a = _evidence()
    request_a = _request(evidence_a, _reconciliation())
    first = _assess(path, request_a, _signed(_statement(request_a, root), private), root)

    request_model_b = evidence_a.request.model_copy(update={"max_age_seconds": 301})
    evidence_b = evidence_a.model_copy(update={"request": request_model_b})
    request_b = _request(
        evidence_b,
        _reconciliation("execution-controlled-test-0002"),
        request_id="external-trust-request-phase18-000002",
        challenge_id="external-trust-challenge-phase18-002",
    )
    second = _assess(
        path,
        request_b,
        _signed(
            _statement(request_b, root, statement_id="phase18-statement-000000000000002"),
            private,
        ),
        root,
    )
    assert first.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED
    assert second.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED


def test_timeout_is_bounded_sanitized_and_never_consumed(tmp_path) -> None:
    private, root = _root()
    request = _request()
    envelope = _signed(_statement(request, root), private)
    release = Event()

    class SlowProvider:
        provider_id = request.expected_provider_id

        def read_attestation(self, _request):
            release.wait(timeout=1)
            return envelope

    store = DurableIndependentReplayStore.initialize(tmp_path / "replay.sqlite3", now=NOW)
    result = AuthenticatedExternalTrustIntegration(
        SlowProvider(), verification_root=root, replay_store=store,
        provider_timeout_seconds=0.01,
    ).assess(request, evaluated_at=NOW)
    release.set()
    assert result.status is ExternalTrustIntegrationStatus.UNAVAILABLE
    assert result.reason_codes == ("EXTERNAL_TRUST_PROVIDER_TIMEOUT",)
    assert result.replay_consumed is False
    assert result.production_authorization_available is False


def test_missing_durable_replay_store_fails_closed() -> None:
    private, root = _root()
    request = _request()
    result = AuthenticatedExternalTrustIntegration(
        _Provider(_signed(_statement(request, root), private)), verification_root=root
    ).assess(request, evaluated_at=NOW)
    assert result.status is ExternalTrustIntegrationStatus.UNAVAILABLE
    assert result.reason_codes == ("DURABLE_EXTERNAL_ATTESTATION_REPLAY_UNAVAILABLE",)
    assert result.replay_consumed is False
