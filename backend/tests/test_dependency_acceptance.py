"""Phase 21 external-dependency acceptance contract tests."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.dependency_acceptance import (
    ApprovalDecision,
    BrokerEvidenceField,
    BrokerEvidencePayload,
    ConsistencyOutcome,
    DependencyAcceptanceContext,
    DependencyAcceptanceRegistry,
    DependencyAcceptanceRegistryEntry,
    DependencyAcceptanceReport,
    DependencyMaturity,
    DeliveryOutcome,
    EvidenceProvenance,
    EvidenceRevocationState,
    EvidenceVerificationResult,
    ExternalDependencyEvidenceEnvelope,
    ExternalDependencyId,
    HumanApprovalPayload,
    IntegrationEnvironment,
    MonitoringDeliveryPayload,
    ReconciliationEvidencePayload,
    RecoveryRehearsalPayload,
    RehearsalOutcome,
    ReviewDecision,
    SecurityReviewPayload,
    TrustDependencyPayload,
)
from app.observability.events import (
    OperationalEventState,
    OperationalReasonCode,
    OperationalSignal,
    operational_event,
)
from app.services.dependency_acceptance import (
    PHASE21_POLICY_VERSION,
    OfflineDependencyAcceptanceEvaluator,
    build_dependency_acceptance_registry,
)


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
FP = "a" * 64
OTHER_FP = "b" * 64
COMMIT = "1" * 40


def _context(**changes) -> DependencyAcceptanceContext:
    values = {
        "environment": IntegrationEnvironment.SYNTHETIC_TEST,
        "subject_id": "friday.release",
        "request_id": "request/phase21",
        "challenge_id": "challenge/phase21",
        "scope_fingerprint": FP,
        "acceptance_policy_version": PHASE21_POLICY_VERSION,
        "commit_hash": COMMIT,
        "evaluated_at": NOW,
    }
    values.update(changes)
    return DependencyAcceptanceContext(**values)


def _evidence(dependency_id: ExternalDependencyId, **changes) -> ExternalDependencyEvidenceEnvelope:
    registry = build_dependency_acceptance_registry()
    entry = next(item for item in registry.entries if item.dependency_id is dependency_id)
    payload = changes.pop("payload", _payload(dependency_id))
    values = {
        "evidence_id": f"fixture/{dependency_id.value}",
        "dependency_id": dependency_id,
        "evidence_type": entry.required_evidence_types[0],
        "issuer_id": "fixture.issuer",
        "subject_id": "friday.release",
        "environment": IntegrationEnvironment.SYNTHETIC_TEST,
        "request_id": "request/phase21",
        "challenge_id": "challenge/phase21",
        "scope_fingerprint": FP,
        "payload_fingerprint": payload.fingerprint,
        "payload": payload,
        "signature_fingerprint": FP,
        "trust_chain_fingerprint": OTHER_FP,
        "issued_at": NOW - timedelta(seconds=10),
        "expires_at": NOW + timedelta(minutes=1),
        "verified_at": NOW - timedelta(seconds=5),
        "source_sequence": 1 if entry.requires_monotonic_sequence else None,
        "previous_source_sequence": 0 if entry.requires_monotonic_sequence else None,
        "provenance": EvidenceProvenance.SYNTHETIC_FIXTURE,
        "verification_result": EvidenceVerificationResult.SYNTHETIC_ONLY,
        "revocation_state": EvidenceRevocationState.ACTIVE,
        "synthetic": True,
        "acceptance_policy_version": PHASE21_POLICY_VERSION,
        "commit_hash": COMMIT,
    }
    values.update(changes)
    return ExternalDependencyEvidenceEnvelope(**values)


def _payload(dependency_id: ExternalDependencyId, **changes):
    if dependency_id in {
        ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
        ExternalDependencyId.LIFECYCLE_REVOCATION_AUTHORITY,
        ExternalDependencyId.MONOTONIC_ROLLBACK_WITNESS,
    }:
        payload = TrustDependencyPayload(
            payload_type={
                ExternalDependencyId.INDEPENDENT_TRUST_ROOT: "trust_root_provisioning",
                ExternalDependencyId.LIFECYCLE_REVOCATION_AUTHORITY: "lifecycle_status",
                ExternalDependencyId.MONOTONIC_ROLLBACK_WITNESS: "rollback_witness_confirmation",
            }[dependency_id],
            lineage_fingerprint=FP,
            authority_identity_fingerprint=OTHER_FP,
        )
    elif dependency_id is ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE:
        payload = BrokerEvidencePayload(
            source_identity_fingerprint=FP,
            account_scope_fingerprint=FP,
            snapshot_id="snapshot/phase21",
            snapshot_generation=1,
            required_fields=tuple(BrokerEvidenceField),
            lineage_fingerprint=OTHER_FP,
            read_only_capability_claimed=True,
            capability_evidence_fingerprint=FP,
            unresolved_conflicts=0,
        )
    elif dependency_id is ExternalDependencyId.AUTHORITATIVE_RECONCILIATION:
        payload = ReconciliationEvidencePayload(
            source_identity_fingerprint=FP,
            snapshot_fingerprint=OTHER_FP,
            snapshot_generation=1,
            expected_record_count=2,
            covered_record_count=2,
            complete=True,
            unresolved_discrepancies=0,
            conflicting_generation=False,
        )
    elif dependency_id is ExternalDependencyId.MONITORING_ALERT_DELIVERY:
        payload = MonitoringDeliveryPayload(
            signal_id="signal/phase21",
            test_event_fingerprint=FP,
            outcome=DeliveryOutcome.DELIVERED,
            receipt_id="receipt/phase21",
            receipt_fingerprint=OTHER_FP,
            event_at=NOW - timedelta(seconds=5),
            destination_fingerprint=FP,
        )
    elif dependency_id is ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL:
        payload = RecoveryRehearsalPayload(
            backup_fingerprint=FP,
            rehearsal_id="rehearsal/phase21",
            started_at=NOW - timedelta(seconds=9),
            completed_at=NOW - timedelta(seconds=8),
            observed_rpo_seconds=30,
            observed_rto_seconds=60,
            maximum_rpo_seconds=60,
            maximum_rto_seconds=120,
            replay_consistency=ConsistencyOutcome.CONSISTENT,
            witness_consistency=ConsistencyOutcome.CONSISTENT,
            outcome=RehearsalOutcome.PASSED,
            evidence_fingerprint=OTHER_FP,
        )
    elif dependency_id is ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW:
        payload = SecurityReviewPayload(
            reviewer_identity_fingerprint=FP,
            reviewer_organization_role="external.reviewer",
            independence_attested=True,
            review_scope_fingerprint=FP,
            target_commit=COMMIT,
            finding_count=0,
            unresolved_critical_count=0,
            unresolved_high_count=0,
            outstanding_blocker_count=0,
            decision=ReviewDecision.APPROVED,
            decision_expires_at=NOW + timedelta(days=1),
            evidence_fingerprint=OTHER_FP,
        )
    else:
        payload = HumanApprovalPayload(
            approver_identity_fingerprint=FP,
            approver_role="production.approver",
            approved_scope_fingerprint=FP,
            target_commit=COMMIT,
            policy_version=PHASE21_POLICY_VERSION,
            issued_at=NOW - timedelta(seconds=10),
            expires_at=NOW + timedelta(minutes=1),
            revocation_state=EvidenceRevocationState.ACTIVE,
            decision_id="approval/phase21",
            decision=ApprovalDecision.APPROVED,
            evidence_fingerprint=OTHER_FP,
        )
    return payload.model_copy(update=changes) if changes else payload


def _assessment(report: DependencyAcceptanceReport, dependency_id: ExternalDependencyId):
    return next(item for item in report.assessments if item.dependency_id is dependency_id)


def _assert_non_authorizing(report: DependencyAcceptanceReport, dependency_id: ExternalDependencyId) -> None:
    item = _assessment(report, dependency_id)
    assert item.independently_verified is False
    assert item.operationally_accepted is False
    assert item.authorizes_execution is False
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_registry_is_complete_deterministic_and_non_authorizing() -> None:
    first = build_dependency_acceptance_registry()
    second = build_dependency_acceptance_registry()

    assert len(first.entries) == len(ExternalDependencyId) == 9
    assert first.entries == second.entries
    assert first.fingerprint == second.fingerprint
    assert first.production_acceptance_available is False
    assert first.authorizes_execution is False
    assert all(item.current_maturity is DependencyMaturity.LOCAL_CONTRACT for item in first.entries)
    assert all(item.blocker_codes == ("REAL_INDEPENDENT_DEPENDENCY_UNAVAILABLE",) for item in first.entries)
    assert {
        item.dependency_id for item in first.entries if item.requires_commit_binding
    } == {
        ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW,
        ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL,
    }


def test_valid_context_is_canonically_reconstructed() -> None:
    context = _context()
    report = OfflineDependencyAcceptanceEvaluator().evaluate(context=context)

    assert report.context == context
    assert report.context is not context
    assert report.status == "blocked"
    assert report.execution_authority is False


@pytest.mark.parametrize(
    "changes",
    (
        {"subject_id": 123},
        {"environment": "synthetic_test"},
        {"evaluated_at": NOW.replace(tzinfo=None)},
        {"scope_fingerprint": "not-a-fingerprint"},
    ),
)
def test_copied_context_with_unvalidated_changes_is_rejected(changes) -> None:
    corrupted = _context().model_copy(update=changes)

    with pytest.raises(ValidationError):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=corrupted)


def test_context_missing_required_data_is_rejected() -> None:
    values = _context().model_dump()
    values.pop("request_id")
    corrupted = DependencyAcceptanceContext.model_construct(**values)

    with pytest.raises(ValueError, match="missing=.*request_id"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=corrupted)


def test_context_subclass_and_custom_object_cannot_bypass_validation() -> None:
    class ContextSubclass(DependencyAcceptanceContext):
        pass

    subclass = ContextSubclass(**_context().model_dump())

    with pytest.raises(ValueError, match="unexpected dependency acceptance context type"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=subclass)
    with pytest.raises(ValueError, match="unexpected dependency acceptance context type"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=object())  # type: ignore[arg-type]


def test_context_with_model_copy_injected_extra_field_is_rejected() -> None:
    corrupted = _context().model_copy(update={"undeclared_context_field": "ignored"})

    with pytest.raises(ValueError, match="undeclared=.*undeclared_context_field"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=corrupted)


def test_shadowed_model_dump_is_never_invoked() -> None:
    calls: list[bool] = []

    def shadowed_model_dump(**_kwargs):
        calls.append(True)
        return _context().model_dump()

    corrupted = _context().model_copy(update={"model_dump": shadowed_model_dump})

    with pytest.raises(ValueError, match="undeclared=.*model_dump"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=corrupted)
    assert calls == []


def test_shadowed_model_dump_cannot_hide_corrupted_required_field() -> None:
    clean = _context()
    calls: list[bool] = []

    def clean_substitute(**_kwargs):
        calls.append(True)
        return clean.model_dump()

    corrupted = clean.model_copy(
        update={"subject_id": 123, "model_dump": clean_substitute}
    )

    with pytest.raises(ValueError, match="undeclared=.*model_dump"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=corrupted)
    assert calls == []


def test_context_with_nonempty_pydantic_extra_state_is_rejected() -> None:
    corrupted = _context().model_copy()
    object.__setattr__(corrupted, "__pydantic_extra__", {"unexpected": "value"})

    with pytest.raises(ValueError, match="contains undeclared extra state"):
        OfflineDependencyAcceptanceEvaluator().evaluate(context=corrupted)


def test_every_dependency_starts_unavailable_and_report_is_structurally_blocked() -> None:
    report = OfflineDependencyAcceptanceEvaluator().evaluate(context=_context())

    assert len(report.assessments) == 9
    assert all(item.maturity is DependencyMaturity.UNAVAILABLE for item in report.assessments)
    assert all(item.reason_codes == ("DEPENDENCY_EVIDENCE_MISSING",) for item in report.assessments)
    assert report.status == "blocked"
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


@pytest.mark.parametrize("dependency_id", tuple(ExternalDependencyId))
def test_synthetic_evidence_never_establishes_independent_acceptance(dependency_id) -> None:
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(_evidence(dependency_id),)
    )
    item = _assessment(report, dependency_id)

    assert item.maturity is DependencyMaturity.SYNTHETIC_TESTED
    assert "SYNTHETIC_EVIDENCE_NOT_OPERATIONAL" in item.reason_codes
    assert item.independently_verified is False
    assert item.operationally_accepted is False
    assert item.authorizes_execution is False
    assert report.production_authorization_available is False


def test_caller_cannot_supply_status_or_authority_booleans() -> None:
    with pytest.raises(ValidationError):
        ExternalDependencyEvidenceEnvelope(
            **{
                **_evidence(ExternalDependencyId.INDEPENDENT_TRUST_ROOT).model_dump(
                    exclude={"envelope_fingerprint"}
                ),
                "status": "accepted",
            }
        )
    with pytest.raises(ValidationError):
        DependencyAcceptanceReport(
            **{
                **OfflineDependencyAcceptanceEvaluator()
                .evaluate(context=_context())
                .model_dump(exclude={"fingerprint"}),
                "production_authorization_available": True,
            }
        )


def test_claimed_independent_verification_remains_blocked_without_real_verifier() -> None:
    evidence = _evidence(
        ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
        synthetic=False,
        provenance=EvidenceProvenance.INDEPENDENT_AUTHORITY,
        verification_result=EvidenceVerificationResult.INDEPENDENTLY_VERIFIED,
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,))
    item = _assessment(report, evidence.dependency_id)

    assert item.maturity is DependencyMaturity.LOCAL_CONTRACT
    assert "INDEPENDENT_VERIFIER_NOT_CONFIGURED" in item.reason_codes
    assert "CLAIMED_INDEPENDENT_VERIFICATION_NOT_TRUSTED_OFFLINE" in item.reason_codes
    assert item.independently_verified is False


def test_caller_selected_issuer_and_signature_metadata_remain_unverified() -> None:
    evidence = _evidence(
        ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
        issuer_id="caller.selected.issuer",
        synthetic=False,
        provenance=EvidenceProvenance.INDEPENDENT_AUTHORITY,
        verification_result=EvidenceVerificationResult.INDEPENDENTLY_VERIFIED,
        signature_fingerprint="c" * 64,
        trust_chain_fingerprint="d" * 64,
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "DEPENDENCY_ISSUER_UNVERIFIED" in item.reason_codes
    assert "INDEPENDENT_VERIFIER_NOT_CONFIGURED" in item.reason_codes
    assert item.independently_verified is False


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"subject_id": "other.release"}, "DEPENDENCY_SUBJECT_MISMATCH"),
        ({"request_id": "request/other"}, "DEPENDENCY_REQUEST_MISMATCH"),
        ({"challenge_id": "challenge/other"}, "DEPENDENCY_CHALLENGE_MISMATCH"),
        ({"environment": IntegrationEnvironment.ISOLATED_SANDBOX}, "DEPENDENCY_ENVIRONMENT_MISMATCH"),
        ({"scope_fingerprint": OTHER_FP}, "DEPENDENCY_SCOPE_MISMATCH"),
        ({"acceptance_policy_version": "phase21/other"}, "DEPENDENCY_POLICY_VERSION_MISMATCH"),
        ({"commit_hash": "2" * 40}, "DEPENDENCY_COMMIT_MISMATCH"),
    ),
)
def test_wrong_subject_environment_scope_policy_or_commit_fails_closed(change, reason) -> None:
    evidence = _evidence(ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL, **change)
    report = OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,))
    item = _assessment(report, evidence.dependency_id)

    assert item.maturity is DependencyMaturity.INVALID
    assert reason in item.reason_codes
    assert report.execution_authority is False


def test_expired_and_future_dated_evidence_fail_closed() -> None:
    expired = _evidence(
        ExternalDependencyId.MONITORING_ALERT_DELIVERY,
        payload=_payload(
            ExternalDependencyId.MONITORING_ALERT_DELIVERY,
            event_at=NOW - timedelta(days=1, hours=12),
        ),
        issued_at=NOW - timedelta(days=2),
        verified_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(days=1),
    )
    future = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        issued_at=NOW + timedelta(minutes=1),
        verified_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=2),
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(expired, future)
    )

    assert _assessment(report, expired.dependency_id).maturity is DependencyMaturity.STALE
    assert "DEPENDENCY_EVIDENCE_STALE" in _assessment(report, expired.dependency_id).reason_codes
    assert "DEPENDENCY_EVIDENCE_FUTURE_DATED" in _assessment(report, future.dependency_id).reason_codes


@pytest.mark.parametrize(
    ("changes", "expected", "reasons"),
    (
        (
            {
                "issued_at": NOW - timedelta(days=2),
                "verified_at": NOW + timedelta(seconds=1),
                "expires_at": NOW - timedelta(days=1),
            },
            DependencyMaturity.INVALID,
            ("DEPENDENCY_EVIDENCE_FUTURE_DATED", "DEPENDENCY_EVIDENCE_STALE"),
        ),
        (
            {
                "signature_fingerprint": None,
                "trust_chain_fingerprint": None,
                "revocation_state": EvidenceRevocationState.REVOKED,
            },
            DependencyMaturity.REVOKED,
            ("DEPENDENCY_SIGNATURE_OR_TRUST_CHAIN_MISSING", "DEPENDENCY_IDENTITY_REVOKED"),
        ),
        (
            {
                "issued_at": NOW - timedelta(days=2),
                "verified_at": NOW - timedelta(days=2),
                "expires_at": NOW - timedelta(days=1),
                "revocation_state": EvidenceRevocationState.REVOKED,
            },
            DependencyMaturity.REVOKED,
            ("DEPENDENCY_EVIDENCE_STALE", "DEPENDENCY_IDENTITY_REVOKED"),
        ),
        (
            {
                "subject_id": "other.release",
                "issued_at": NOW - timedelta(days=2),
                "verified_at": NOW - timedelta(days=2),
                "expires_at": NOW - timedelta(days=1),
            },
            DependencyMaturity.INVALID,
            ("DEPENDENCY_SUBJECT_MISMATCH", "DEPENDENCY_EVIDENCE_STALE"),
        ),
        (
            {
                "issued_at": NOW + timedelta(seconds=1),
                "verified_at": NOW + timedelta(seconds=1),
                "expires_at": NOW + timedelta(minutes=1),
            },
            DependencyMaturity.INVALID,
            ("DEPENDENCY_EVIDENCE_FUTURE_DATED",),
        ),
        (
            {
                "issued_at": NOW - timedelta(days=2),
                "verified_at": NOW - timedelta(days=2),
                "expires_at": NOW - timedelta(days=1),
            },
            DependencyMaturity.STALE,
            ("DEPENDENCY_EVIDENCE_STALE",),
        ),
        (
            {"revocation_state": EvidenceRevocationState.UNKNOWN},
            DependencyMaturity.UNAVAILABLE,
            ("DEPENDENCY_REVOCATION_UNKNOWN",),
        ),
        (
            {"revocation_state": EvidenceRevocationState.REVOKED},
            DependencyMaturity.REVOKED,
            ("DEPENDENCY_IDENTITY_REVOKED",),
        ),
        (
            {
                "issued_at": NOW + timedelta(seconds=1),
                "verified_at": NOW + timedelta(seconds=1),
                "expires_at": NOW + timedelta(minutes=1),
                "revocation_state": EvidenceRevocationState.REVOKED,
            },
            DependencyMaturity.REVOKED,
            ("DEPENDENCY_EVIDENCE_FUTURE_DATED", "DEPENDENCY_IDENTITY_REVOKED"),
        ),
    ),
)
def test_general_maturity_precedence_is_explicit_and_deterministic(
    changes, expected, reasons
) -> None:
    dependency_id = ExternalDependencyId.INDEPENDENT_TRUST_ROOT
    evidence = _evidence(dependency_id, **changes)
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(evidence,)
    )
    item = _assessment(report, dependency_id)

    assert item.maturity is expected
    assert set(reasons).issubset(item.reason_codes)
    contradictory_reasons = {
        DependencyMaturity.REVOKED: ("DEPENDENCY_REVOCATION_UNKNOWN",),
        DependencyMaturity.INVALID: (
            "DEPENDENCY_IDENTITY_REVOKED",
            "DEPENDENCY_REVOCATION_UNKNOWN",
        ),
        DependencyMaturity.STALE: (
            "DEPENDENCY_EVIDENCE_FUTURE_DATED",
            "DEPENDENCY_IDENTITY_REVOKED",
            "DEPENDENCY_REVOCATION_UNKNOWN",
        ),
        DependencyMaturity.UNAVAILABLE: (
            "DEPENDENCY_EVIDENCE_FUTURE_DATED",
            "DEPENDENCY_EVIDENCE_STALE",
            "DEPENDENCY_IDENTITY_REVOKED",
        ),
    }
    assert set(contradictory_reasons[expected]).isdisjoint(item.reason_codes)
    _assert_non_authorizing(report, dependency_id)


@pytest.mark.parametrize(
    "state",
    (
        EvidenceRevocationState.REVOKED,
        EvidenceRevocationState.SUPERSEDED,
        EvidenceRevocationState.COMPROMISED,
    ),
)
def test_terminal_identity_states_fail_closed(state) -> None:
    evidence = _evidence(ExternalDependencyId.LIFECYCLE_REVOCATION_AUTHORITY, revocation_state=state)
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert item.maturity is DependencyMaturity.REVOKED
    assert "DEPENDENCY_IDENTITY_REVOKED" in item.reason_codes


def test_unknown_revocation_and_witness_outcome_remain_blocked() -> None:
    evidence = _evidence(
        ExternalDependencyId.MONOTONIC_ROLLBACK_WITNESS,
        revocation_state=EvidenceRevocationState.UNKNOWN,
        synthetic=False,
        provenance=EvidenceProvenance.LOCAL_CONTRACT,
        verification_result=EvidenceVerificationResult.UNAVAILABLE,
        reason_codes=("WITNESS_CONFIRMATION_UNKNOWN",),
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "DEPENDENCY_REVOCATION_UNKNOWN" in item.reason_codes
    assert "WITNESS_CONFIRMATION_UNKNOWN" not in item.reason_codes
    assert "CLAIMED_VERIFICATION_UNAVAILABLE" in item.reason_codes
    assert item.operationally_accepted is False


def test_duplicate_and_conflicting_evidence_are_distinct_failures() -> None:
    first = _evidence(ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE)
    duplicate = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(first, first)
    )
    second = _evidence(
        ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
        evidence_id="fixture/broker/second",
        payload=_payload(
            ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
            snapshot_generation=2,
        ),
    )
    conflicting = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(first, second)
    )

    assert "DUPLICATE_DEPENDENCY_EVIDENCE" in _assessment(duplicate, first.dependency_id).reason_codes
    assert _assessment(conflicting, first.dependency_id).maturity is DependencyMaturity.CONFLICTING
    assert "CONFLICTING_DEPENDENCY_EVIDENCE" in _assessment(conflicting, first.dependency_id).reason_codes


def test_reused_evidence_id_with_changed_payload_is_rejected_as_duplicate() -> None:
    first = _evidence(ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE)
    changed = _evidence(
        ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
        payload=_payload(
            ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
            snapshot_generation=2,
        ),
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(first, changed)
    )
    item = _assessment(report, first.dependency_id)
    assert item.maturity is DependencyMaturity.INVALID
    assert "DUPLICATE_DEPENDENCY_EVIDENCE" in item.reason_codes


def test_missing_signature_and_sequence_fail_closed() -> None:
    evidence = _evidence(ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL)
    malformed = evidence.model_copy(
        update={
            "signature_fingerprint": None,
            "trust_chain_fingerprint": None,
            "source_sequence": None,
            "previous_source_sequence": None,
        }
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(malformed,)),
        evidence.dependency_id,
    )
    assert "DEPENDENCY_SIGNATURE_OR_TRUST_CHAIN_MISSING" in item.reason_codes
    assert "DEPENDENCY_SEQUENCE_MISSING" in item.reason_codes


def test_malformed_fingerprint_and_sequence_gap_are_rejected_at_model_boundary() -> None:
    with pytest.raises(ValidationError):
        _evidence(ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE, payload_fingerprint="bad")
    with pytest.raises(ValidationError, match="advance exactly once"):
        _evidence(
            ExternalDependencyId.MONOTONIC_ROLLBACK_WITNESS,
            source_sequence=4,
            previous_source_sequence=1,
        )


def test_typed_broker_and_reconciliation_incompleteness_is_evaluated_locally() -> None:
    broker = _evidence(
        ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
        payload=_payload(
            ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
            required_fields=(BrokerEvidenceField.ACCOUNT_STATE,),
            read_only_capability_claimed=False,
            capability_evidence_fingerprint=None,
        ),
    )
    reconciliation = _evidence(
        ExternalDependencyId.AUTHORITATIVE_RECONCILIATION,
        payload=_payload(
            ExternalDependencyId.AUTHORITATIVE_RECONCILIATION,
            complete=False,
            covered_record_count=1,
            unresolved_discrepancies=1,
            conflicting_generation=True,
        ),
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(broker, reconciliation)
    )
    assert "BROKER_EVIDENCE_INCOMPLETE" in _assessment(report, broker.dependency_id).reason_codes
    assert "BROKER_READ_ONLY_CAPABILITY_MISSING" in _assessment(report, broker.dependency_id).reason_codes
    assert "RECONCILIATION_DISCREPANCY" in _assessment(report, reconciliation.dependency_id).reason_codes
    assert "RECONCILIATION_COVERAGE_INCOMPLETE" in _assessment(
        report, reconciliation.dependency_id
    ).reason_codes


def test_monitoring_delivery_requires_typed_receipt_and_remains_unverified() -> None:
    payload = _payload(
        ExternalDependencyId.MONITORING_ALERT_DELIVERY,
        outcome=DeliveryOutcome.UNKNOWN,
        receipt_id=None,
        receipt_fingerprint=None,
    )
    evidence = _evidence(ExternalDependencyId.MONITORING_ALERT_DELIVERY, payload=payload)
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "MONITORING_DELIVERY_NOT_CONFIRMED" in item.reason_codes
    assert "MONITORING_DELIVERY_RECEIPT_UNVERIFIED" in item.reason_codes
    assert item.maturity is DependencyMaturity.INVALID


def test_recovery_rehearsal_requires_completion_objectives_and_consistency() -> None:
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        completed_at=None,
        observed_rpo_seconds=None,
        observed_rto_seconds=None,
        replay_consistency=ConsistencyOutcome.UNKNOWN,
        witness_consistency=ConsistencyOutcome.INCONSISTENT,
        outcome=RehearsalOutcome.INCOMPLETE,
    )
    evidence = _evidence(ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL, payload=payload)
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "RECOVERY_REHEARSAL_INCOMPLETE" in item.reason_codes
    assert "RECOVERY_REPLAY_WITNESS_INCONSISTENT" in item.reason_codes
    assert "RECOVERY_REHEARSAL_UNVERIFIED" in item.reason_codes


def test_security_review_requires_independence_no_blockers_and_exact_release() -> None:
    payload = _payload(
        ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW,
        independence_attested=False,
        target_commit="2" * 40,
        unresolved_high_count=1,
        outstanding_blocker_count=1,
        decision=ReviewDecision.REMEDIATION_REQUIRED,
    )
    evidence = _evidence(ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW, payload=payload)
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "SECURITY_REVIEW_NOT_INDEPENDENT" in item.reason_codes
    assert "SECURITY_REVIEW_COMMIT_MISMATCH" in item.reason_codes
    assert "SECURITY_REVIEW_BLOCKERS_UNRESOLVED" in item.reason_codes
    assert "REVIEWER_INDEPENDENCE_UNVERIFIED" in item.reason_codes


def test_human_approval_requires_exact_active_unexpired_decision() -> None:
    issued = NOW - timedelta(minutes=2)
    expired = NOW - timedelta(minutes=1)
    payload = _payload(
        ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL,
        target_commit="2" * 40,
        policy_version="phase21/other",
        issued_at=issued,
        expires_at=expired,
        revocation_state=EvidenceRevocationState.REVOKED,
        decision=ApprovalDecision.DENIED,
    )
    evidence = _evidence(
        ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL,
        payload=payload,
        issued_at=issued,
        expires_at=expired,
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "APPROVAL_COMMIT_MISMATCH" in item.reason_codes
    assert "APPROVAL_POLICY_MISMATCH" in item.reason_codes
    assert "APPROVAL_EXPIRED" in item.reason_codes
    assert "APPROVAL_REVOKED_OR_UNKNOWN" in item.reason_codes
    assert "APPROVAL_NOT_GRANTED" in item.reason_codes


@pytest.mark.parametrize(
    "dependency_id",
    (
        ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW,
        ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL,
    ),
)
def test_release_bound_evidence_rejects_missing_commit(dependency_id) -> None:
    evidence = _evidence(dependency_id, commit_hash=None)
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        dependency_id,
    )
    assert item.maturity is DependencyMaturity.INVALID
    assert "DEPENDENCY_COMMIT_BINDING_MISSING" in item.reason_codes


def test_release_binding_rejects_malformed_commit_at_model_boundary() -> None:
    with pytest.raises(ValidationError):
        _evidence(ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL, commit_hash="bad")


def test_payload_fingerprint_is_recomputed_and_must_match() -> None:
    with pytest.raises(ValidationError, match="payload fingerprint"):
        _evidence(ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE, payload_fingerprint=FP)


def test_caller_reason_codes_are_not_treated_as_policy_results() -> None:
    evidence = _evidence(
        ExternalDependencyId.AUTHORITATIVE_RECONCILIATION,
        synthetic=False,
        provenance=EvidenceProvenance.MANUAL_CLAIM,
        verification_result=EvidenceVerificationResult.INVALID,
        reason_codes=("CALLER_ASSERTED_ACCEPTED",),
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        evidence.dependency_id,
    )
    assert "CALLER_ASSERTED_ACCEPTED" not in item.reason_codes
    assert "CLAIMED_VERIFICATION_INVALID" in item.reason_codes


def test_registry_rejects_missing_duplicate_and_out_of_order_entries() -> None:
    registry = build_dependency_acceptance_registry()
    with pytest.raises(ValidationError, match="every dependency exactly once"):
        DependencyAcceptanceRegistry(
            policy_version=registry.policy_version,
            entries=registry.entries[:-1],
        )
    with pytest.raises(ValidationError, match="every dependency exactly once"):
        DependencyAcceptanceRegistry(
            policy_version=registry.policy_version,
            entries=registry.entries[:-1] + (registry.entries[0],),
        )
    with pytest.raises(ValidationError, match="ordering"):
        DependencyAcceptanceRegistry(
            policy_version=registry.policy_version,
            entries=tuple(reversed(registry.entries)),
        )


def test_registry_rejects_malformed_owner_and_custom_injection_is_ignored() -> None:
    registry = build_dependency_acceptance_registry()
    first_data = registry.entries[0].model_dump(exclude={"fingerprint"})
    first_data["owner_role"] = ""
    with pytest.raises(ValidationError):
        DependencyAcceptanceRegistryEntry.model_validate(first_data)

    missing_blocker = registry.entries[0].model_dump(exclude={"fingerprint"})
    missing_blocker["blocker_codes"] = ("SOME_OTHER_BLOCKER",)
    with pytest.raises(ValidationError, match="external blocker"):
        DependencyAcceptanceRegistryEntry.model_validate(missing_blocker)

    invalid_environments = registry.entries[0].model_dump(exclude={"fingerprint"})
    invalid_environments["allowed_environments"] = (IntegrationEnvironment.PRODUCTION,)
    with pytest.raises(ValidationError, match="local and synthetic"):
        DependencyAcceptanceRegistryEntry.model_validate(invalid_environments)

    invalid_mode = registry.entries[0].model_dump(exclude={"fingerprint"})
    invalid_mode["verification_mode"] = "caller_defined"
    with pytest.raises(ValidationError):
        DependencyAcceptanceRegistryEntry.model_validate(invalid_mode)

    missing_criteria = registry.entries[0].model_dump(exclude={"fingerprint"})
    missing_criteria["acceptance_criteria"] = ()
    with pytest.raises(ValidationError):
        DependencyAcceptanceRegistryEntry.model_validate(missing_criteria)

    custom_entry = registry.entries[0].model_copy(update={"owner_role": "caller.claim"})
    custom = DependencyAcceptanceRegistry(
        policy_version=registry.policy_version,
        entries=(custom_entry,) + registry.entries[1:],
    )
    evaluator = OfflineDependencyAcceptanceEvaluator(registry=custom)
    report = evaluator.evaluate(context=_context())
    assert report.registry_fingerprint == registry.fingerprint


def test_registry_equality_spoof_cannot_replace_canonical_policy() -> None:
    canonical = build_dependency_acceptance_registry()
    weakened_entries = tuple(
        entry.model_copy(
            update={
                "requires_commit_binding": False,
                "requires_signature": False,
                "requires_monotonic_sequence": False,
                "freshness_seconds": 31_536_000,
            }
        )
        for entry in canonical.entries
    )

    class EqualitySpoof:
        entries = weakened_entries
        fingerprint = "f" * 64

        def __eq__(self, other) -> bool:
            return True

    evaluator = OfflineDependencyAcceptanceEvaluator(registry=EqualitySpoof())  # type: ignore[arg-type]
    report = evaluator.evaluate(
        context=_context(),
        evidence=(
            _evidence(ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW, commit_hash=None),
        ),
    )

    assert type(evaluator._registry) is DependencyAcceptanceRegistry
    assert evaluator._registry == canonical
    assert report.registry_fingerprint == canonical.fingerprint
    assert "DEPENDENCY_COMMIT_BINDING_MISSING" in _assessment(
        report, ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW
    ).reason_codes


def test_registry_equality_exception_is_never_invoked() -> None:
    class ExplosiveEquality:
        def __eq__(self, other) -> bool:
            raise AssertionError("caller equality must not run")

    evaluator = OfflineDependencyAcceptanceEvaluator(registry=ExplosiveEquality())  # type: ignore[arg-type]
    report = evaluator.evaluate(context=_context())

    assert type(evaluator._registry) is DependencyAcceptanceRegistry
    assert report.registry_fingerprint == build_dependency_acceptance_registry().fingerprint


@pytest.mark.parametrize(
    ("payload_changes", "evidence_changes", "reason"),
    (
        (
            {
                "started_at": NOW + timedelta(seconds=1),
                "completed_at": NOW + timedelta(seconds=2),
            },
            {},
            "RECOVERY_REHEARSAL_FUTURE_DATED",
        ),
        (
            {"completed_at": NOW + timedelta(seconds=1)},
            {},
            "RECOVERY_REHEARSAL_FUTURE_DATED",
        ),
        (
            {
                "started_at": NOW - timedelta(seconds=30),
                "completed_at": NOW - timedelta(seconds=20),
            },
            {},
            "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW",
        ),
        (
            {"completed_at": NOW + timedelta(seconds=30)},
            {"expires_at": NOW + timedelta(seconds=20)},
            "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW",
        ),
    ),
)
def test_recovery_rehearsal_future_and_window_violations_are_invalid(
    payload_changes, evidence_changes, reason
) -> None:
    payload = _payload(ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL, **payload_changes)
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        **evidence_changes,
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(
            context=_context(), evidence=(evidence,)
        ),
        evidence.dependency_id,
    )

    assert item.maturity is DependencyMaturity.INVALID
    assert reason in item.reason_codes
    assert item.authorizes_execution is False


def test_stale_recovery_rehearsal_cannot_be_refreshed_by_new_envelope() -> None:
    completed_at = NOW - timedelta(days=30, seconds=1)
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
    )
    evaluator = OfflineDependencyAcceptanceEvaluator()

    first = evaluator.evaluate(context=_context(), evidence=(evidence,))
    second = evaluator.evaluate(context=_context(), evidence=(evidence,))
    item = _assessment(first, evidence.dependency_id)

    assert first == second
    assert item.maturity is DependencyMaturity.INVALID
    assert "RECOVERY_REHEARSAL_STALE" in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" in item.reason_codes
    assert item.operationally_accepted is False


def test_recent_recovery_rehearsal_inside_envelope_is_locally_valid() -> None:
    evidence = _evidence(ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL)
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(
            context=_context(), evidence=(evidence,)
        ),
        evidence.dependency_id,
    )

    assert item.maturity is DependencyMaturity.SYNTHETIC_TESTED
    assert "RECOVERY_REHEARSAL_FUTURE_DATED" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_STALE" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_UNVERIFIED" in item.reason_codes


def test_recovery_rehearsal_exact_freshness_cutoff_is_inclusive() -> None:
    completed_at = NOW - timedelta(days=30)
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        issued_at=completed_at,
        verified_at=completed_at,
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(
            context=_context(), evidence=(evidence,)
        ),
        evidence.dependency_id,
    )

    assert item.maturity is DependencyMaturity.SYNTHETIC_TESTED
    assert "RECOVERY_REHEARSAL_STALE" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" not in item.reason_codes


def test_stale_envelope_cannot_mask_future_recovery_completion() -> None:
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=NOW - timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=1),
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        issued_at=NOW - timedelta(days=40),
        verified_at=NOW - timedelta(days=39),
        expires_at=NOW - timedelta(days=1),
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(evidence,)
    )
    item = _assessment(report, evidence.dependency_id)

    assert item.maturity is DependencyMaturity.INVALID
    assert "DEPENDENCY_EVIDENCE_STALE" in item.reason_codes
    assert "RECOVERY_REHEARSAL_FUTURE_DATED" in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" in item.reason_codes
    _assert_non_authorizing(report, evidence.dependency_id)


def test_stale_envelope_cannot_mask_recovery_window_violation() -> None:
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=NOW - timedelta(hours=13),
        completed_at=NOW - timedelta(hours=12),
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        issued_at=NOW - timedelta(days=40),
        verified_at=NOW - timedelta(days=39),
        expires_at=NOW - timedelta(days=1),
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(
            context=_context(), evidence=(evidence,)
        ),
        evidence.dependency_id,
    )

    assert item.maturity is DependencyMaturity.INVALID
    assert "DEPENDENCY_EVIDENCE_STALE" in item.reason_codes
    assert "RECOVERY_REHEARSAL_FUTURE_DATED" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" in item.reason_codes


def test_stale_envelope_with_otherwise_valid_old_recovery_remains_stale() -> None:
    completed_at = NOW - timedelta(days=30, seconds=1)
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        issued_at=NOW - timedelta(days=31),
        verified_at=NOW - timedelta(days=30, hours=12),
        expires_at=NOW - timedelta(seconds=1),
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(
            context=_context(), evidence=(evidence,)
        ),
        evidence.dependency_id,
    )

    assert item.maturity is DependencyMaturity.STALE
    assert "DEPENDENCY_EVIDENCE_STALE" in item.reason_codes
    assert "RECOVERY_REHEARSAL_STALE" in item.reason_codes
    assert "RECOVERY_REHEARSAL_FUTURE_DATED" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" not in item.reason_codes


def test_revocation_remains_terminal_over_invalid_recovery_timestamps() -> None:
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        revocation_state=EvidenceRevocationState.REVOKED,
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=(evidence,)
    )
    item = _assessment(report, evidence.dependency_id)

    assert item.maturity is DependencyMaturity.REVOKED
    assert "DEPENDENCY_IDENTITY_REVOKED" in item.reason_codes
    assert "RECOVERY_REHEARSAL_FUTURE_DATED" in item.reason_codes
    _assert_non_authorizing(report, evidence.dependency_id)


def test_recovery_completion_at_envelope_expiry_is_inside_inclusive_window() -> None:
    payload = _payload(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        started_at=NOW - timedelta(seconds=1),
        completed_at=NOW,
    )
    evidence = _evidence(
        ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
        payload=payload,
        expires_at=NOW,
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(
            context=_context(), evidence=(evidence,)
        ),
        evidence.dependency_id,
    )

    # The recovery window is inclusive.  The envelope itself is stale at its
    # exact expiry under the established expires_at <= evaluated_at rule.
    assert item.maturity is DependencyMaturity.STALE
    assert "DEPENDENCY_EVIDENCE_STALE" in item.reason_codes
    assert "RECOVERY_REHEARSAL_FUTURE_DATED" not in item.reason_codes
    assert "RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW" not in item.reason_codes


@pytest.mark.parametrize(
    ("dependency_id", "reason"),
    (
        (ExternalDependencyId.MONITORING_ALERT_DELIVERY, "ALERT_DELIVERY_UNVERIFIED"),
        (ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL, "RECOVERY_REHEARSAL_MISSING"),
        (ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW, "REVIEWER_NOT_INDEPENDENT"),
        (ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL, "APPROVAL_REVOKED"),
    ),
)
def test_manual_operational_prerequisites_remain_blocked(dependency_id, reason) -> None:
    evidence = _evidence(
        dependency_id,
        synthetic=False,
        provenance=EvidenceProvenance.MANUAL_CLAIM,
        verification_result=EvidenceVerificationResult.INVALID,
        reason_codes=(reason,),
    )
    item = _assessment(
        OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=(evidence,)),
        dependency_id,
    )
    assert "CLAIMED_VERIFICATION_INVALID" in item.reason_codes
    assert reason not in item.reason_codes
    assert "CALLER_EVIDENCE_NOT_AUTHORITATIVE" in item.reason_codes


def test_production_environment_is_always_unavailable_in_phase21() -> None:
    context = _context(environment=IntegrationEnvironment.PRODUCTION)
    evidence = _evidence(
        ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
        environment=IntegrationEnvironment.PRODUCTION,
        synthetic=False,
        provenance=EvidenceProvenance.INDEPENDENT_AUTHORITY,
        verification_result=EvidenceVerificationResult.INDEPENDENTLY_VERIFIED,
    )
    report = OfflineDependencyAcceptanceEvaluator().evaluate(context=context, evidence=(evidence,))
    item = _assessment(report, evidence.dependency_id)
    assert "PHASE21_PRODUCTION_ENVIRONMENT_UNAVAILABLE" in item.reason_codes
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_report_is_deterministic_immutable_and_secret_safe() -> None:
    evidence = tuple(_evidence(item) for item in ExternalDependencyId)
    first = OfflineDependencyAcceptanceEvaluator().evaluate(context=_context(), evidence=evidence)
    second = OfflineDependencyAcceptanceEvaluator().evaluate(
        context=_context(), evidence=tuple(reversed(evidence))
    )
    assert first == second
    assert first.fingerprint == second.fingerprint
    with pytest.raises(ValidationError):
        first.execution_authority = True
    with pytest.raises(ValidationError):
        _evidence(
            ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
            issuer_id="token=should-not-appear",
        )


def test_operational_vocabulary_accepts_only_exact_enums_for_phase21_signals() -> None:
    event = operational_event(
        timestamp=NOW,
        signal=OperationalSignal.DEPENDENCY_EVIDENCE_STATUS,
        state=OperationalEventState.BLOCKED,
        reason_code=OperationalReasonCode.DEPENDENCY_EVIDENCE_MISSING,
    )
    assert event.event_type == "dependency_evidence_status"
    assert event.reason_code == "DEPENDENCY_EVIDENCE_MISSING"

    class ForeignSignal(str, Enum):
        DEPENDENCY_EVIDENCE_STATUS = "dependency_evidence_status"

    with pytest.raises(ValueError, match="bounded vocabulary"):
        operational_event(
            timestamp=NOW,
            signal=ForeignSignal.DEPENDENCY_EVIDENCE_STATUS,  # type: ignore[arg-type]
            state=OperationalEventState.BLOCKED,
            reason_code=OperationalReasonCode.DEPENDENCY_EVIDENCE_MISSING,
        )


def test_phase21_service_has_no_network_provider_or_execution_imports() -> None:
    path = Path("app/services/dependency_acceptance.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    prohibited_modules = {
        "requests",
        "httpx",
        "socket",
        "MetaTrader5",
    }
    prohibited_symbols = {
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "order_send",
    }
    assert imported_modules.isdisjoint(prohibited_modules)
    assert all(symbol not in source for symbol in prohibited_symbols)


def test_phase21_is_only_consumed_by_offline_acceptance_and_verification_modules() -> None:
    offline_dependency_paths = {
        Path("app/models/dependency_acceptance.py"),
        Path("app/services/dependency_acceptance.py"),
        Path("app/models/independent_evidence_verification.py"),
        Path("app/services/independent_evidence_verification.py"),
    }
    for path in Path("app").rglob("*.py"):
        if path not in offline_dependency_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            modules = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            assert all("dependency_acceptance" not in module for module in modules)
