"""Offline, non-authorizing Phase 21 dependency acceptance workflow."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from app.models.dependency_acceptance import (
    ApprovalDecision,
    BrokerEvidenceField,
    BrokerEvidencePayload,
    ConsistencyOutcome,
    DependencyAcceptanceAssessment,
    DependencyAcceptanceContext,
    DependencyAcceptanceRegistry,
    DependencyAcceptanceRegistryEntry,
    DependencyAcceptanceReport,
    DependencyEvidenceType,
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
    VerificationMode,
)


PHASE21_POLICY_VERSION = "phase21-external-dependency-acceptance/v1"


# General single-envelope maturity is resolved once from explicit candidates.
# Terminal revocation is most important to retain, invalid evidence cannot be
# softened by age, stale evidence remains distinct from unavailable evidence,
# and none of these states can become acceptance.
_GENERAL_MATURITY_PRECEDENCE = {
    DependencyMaturity.LOCAL_CONTRACT: 0,
    DependencyMaturity.UNAVAILABLE: 1,
    DependencyMaturity.STALE: 2,
    DependencyMaturity.INVALID: 3,
    DependencyMaturity.REVOKED: 4,
}


def build_dependency_acceptance_registry() -> DependencyAcceptanceRegistry:
    """Return the canonical local registry; it is policy metadata, not authority."""

    entries = (
        _entry(
            ExternalDependencyId.INDEPENDENT_TRUST_ROOT,
            DependencyEvidenceType.TRUST_ROOT_PROVISIONING,
            "Pins the independently provisioned root used to authenticate authority keys.",
            "A root supplied by the statement being verified is never trusted.",
            "independent trust administrator and reviewer",
            "Out-of-band fingerprint comparison plus authenticated rotation and revocation history.",
            ("ROOT_IDENTITY_MISMATCH", "ROOT_REVOKED", "ROOT_PROVISIONING_UNVERIFIED"),
            ("independent provisioning", "approved algorithm", "rotation and revocation verified"),
            VerificationMode.MANUAL_INDEPENDENT,
            86_400,
            True,
            True,
        ),
        _entry(
            ExternalDependencyId.LIFECYCLE_REVOCATION_AUTHORITY,
            DependencyEvidenceType.LIFECYCLE_STATUS,
            "Provides authoritative current key lifecycle and revocation state.",
            "Local records and provider assertions cannot clear revocation or compromise.",
            "lifecycle authority operator",
            "Authenticate issuer, exact subject and scope, freshness, sequence, and outage policy.",
            ("LIFECYCLE_UNAVAILABLE", "REVOCATION_UNKNOWN", "LIFECYCLE_SEQUENCE_INVALID"),
            ("authenticated issuer", "fresh authoritative status", "revocation enforced"),
            VerificationMode.AUTOMATED_EXTERNAL,
            300,
            True,
            True,
        ),
        _entry(
            ExternalDependencyId.MONOTONIC_ROLLBACK_WITNESS,
            DependencyEvidenceType.ROLLBACK_WITNESS_CONFIRMATION,
            "Detects replay checkpoint rollback outside the local database trust boundary.",
            "Local SQLite durability is not independent rollback assurance.",
            "independent witness administrator",
            "Verify authenticated challenge-bound monotonic checkpoint confirmation and conflicts.",
            ("WITNESS_UNAVAILABLE", "WITNESS_CONFLICT", "SEQUENCE_ROLLBACK_OR_GAP"),
            ("independent administration", "monotonic confirmation", "exact stream binding"),
            VerificationMode.AUTOMATED_EXTERNAL,
            300,
            True,
            True,
        ),
        _entry(
            ExternalDependencyId.READ_ONLY_BROKER_EVIDENCE,
            DependencyEvidenceType.BROKER_SNAPSHOT,
            "Supplies authenticated, complete, fresh account and instrument evidence.",
            "The source must have no order or account-mutation capability.",
            "broker evidence integration owner",
            "Authenticate source and bind complete snapshot lineage to exact account scope.",
            ("BROKER_SOURCE_UNAUTHENTICATED", "BROKER_EVIDENCE_INCOMPLETE", "BROKER_EVIDENCE_STALE"),
            ("read-only source", "complete required fields", "fresh exact scope"),
            VerificationMode.AUTOMATED_EXTERNAL,
            30,
            True,
            True,
        ),
        _entry(
            ExternalDependencyId.AUTHORITATIVE_RECONCILIATION,
            DependencyEvidenceType.RECONCILIATION_DECISION,
            "Resolves external order-state ambiguity against an authoritative source.",
            "Unresolved discrepancies always block progression and retry.",
            "reconciliation authority operator",
            "Authenticate complete snapshot and generation coverage; reject unresolved differences.",
            ("RECONCILIATION_UNAVAILABLE", "RECONCILIATION_INCOMPLETE", "RECONCILIATION_DISCREPANCY"),
            ("authoritative source", "complete coverage", "no unresolved discrepancy"),
            VerificationMode.AUTOMATED_EXTERNAL,
            60,
            True,
            True,
        ),
        _entry(
            ExternalDependencyId.MONITORING_ALERT_DELIVERY,
            DependencyEvidenceType.ALERT_DELIVERY_RECEIPT,
            "Proves bounded operational signals reach an owned alerting path.",
            "An emitted local event is not proof that an operator received an alert.",
            "monitoring and incident-response owner",
            "Validate authenticated delivery receipt, coverage, escalation owner, and test alert.",
            ("ALERT_DELIVERY_UNVERIFIED", "SIGNAL_COVERAGE_INCOMPLETE", "ALERT_OWNER_UNAVAILABLE"),
            ("bounded signal coverage", "verified delivery receipt", "owned escalation path"),
            VerificationMode.HYBRID,
            86_400,
            True,
            False,
        ),
        _entry(
            ExternalDependencyId.BACKUP_RECOVERY_REHEARSAL,
            DependencyEvidenceType.RECOVERY_REHEARSAL_RESULT,
            "Demonstrates tested recovery within approved recovery objectives.",
            "Backup existence alone does not prove restoration or replay consistency.",
            "recovery and continuity owner",
            "Review authenticated rehearsal results, integrity, RPO/RTO, and witness consistency.",
            ("RECOVERY_REHEARSAL_MISSING", "BACKUP_INTEGRITY_INVALID", "RECOVERY_OBJECTIVE_MISSED"),
            ("successful restore rehearsal", "integrity verified", "replay consistency checked"),
            VerificationMode.HYBRID,
            2_592_000,
            True,
            False,
        ),
        _entry(
            ExternalDependencyId.INDEPENDENT_SECURITY_REVIEW,
            DependencyEvidenceType.SECURITY_REVIEW_DECISION,
            "Records an independent review decision and remediation disposition.",
            "Project self-review cannot count as independent acceptance.",
            "independent security reviewer",
            "Authenticate reviewer independence, reviewed commit and scope, findings, and decision.",
            ("INDEPENDENT_REVIEW_MISSING", "REVIEWER_NOT_INDEPENDENT", "REVIEW_FINDINGS_UNRESOLVED"),
            ("independent reviewer", "exact commit and scope", "blocking findings resolved"),
            VerificationMode.MANUAL_INDEPENDENT,
            2_592_000,
            True,
            False,
            True,
        ),
        _entry(
            ExternalDependencyId.HUMAN_PRODUCTION_APPROVAL,
            DependencyEvidenceType.HUMAN_APPROVAL_DECISION,
            "Records explicit, scoped, expiring human authorization for a reviewed release.",
            "Approval evidence never changes execution controls in Phase 21.",
            "authorized production approver",
            "Authenticate role, exact commit/scope/policy, expiry, and current revocation state.",
            ("PRODUCTION_APPROVAL_MISSING", "APPROVAL_EXPIRED", "APPROVAL_REVOKED"),
            ("authorized approver", "exact release binding", "unexpired and unrevoked decision"),
            VerificationMode.MANUAL_INDEPENDENT,
            86_400,
            True,
            True,
            True,
        ),
    )
    return DependencyAcceptanceRegistry(
        policy_version=PHASE21_POLICY_VERSION,
        entries=tuple(sorted(entries, key=lambda entry: entry.dependency_id.value)),
    )


class OfflineDependencyAcceptanceEvaluator:
    """Validate offline evidence structure while refusing external acceptance."""

    def __init__(self, *, registry: DependencyAcceptanceRegistry | None = None) -> None:
        # Preserve the compatibility parameter without consulting caller-controlled
        # equality or contents.  The active policy is always the canonical registry.
        self._registry = build_dependency_acceptance_registry()

    def evaluate(
        self,
        *,
        context: DependencyAcceptanceContext,
        evidence: tuple[ExternalDependencyEvidenceEnvelope, ...] = (),
    ) -> DependencyAcceptanceReport:
        canonical_context = _canonical_context(context)
        grouped: dict[ExternalDependencyId, list[ExternalDependencyEvidenceEnvelope]] = defaultdict(list)
        for item in evidence:
            grouped[item.dependency_id].append(item)

        assessments = tuple(
            self._assess(entry, grouped[entry.dependency_id], canonical_context)
            for entry in self._registry.entries
        )
        blockers = tuple(sorted({code for item in assessments for code in item.reason_codes}))
        return DependencyAcceptanceReport(
            context=canonical_context,
            registry_fingerprint=self._registry.fingerprint,
            assessments=assessments,
            blocker_codes=blockers,
        )

    def _assess(
        self,
        entry: DependencyAcceptanceRegistryEntry,
        items: list[ExternalDependencyEvidenceEnvelope],
        context: DependencyAcceptanceContext,
    ) -> DependencyAcceptanceAssessment:
        now = context.evaluated_at
        if not items:
            return _assessment(entry, DependencyMaturity.UNAVAILABLE, ("DEPENDENCY_EVIDENCE_MISSING",), now)
        if len(items) != 1:
            fingerprints = _safe_fingerprints(items)
            evidence_ids = [getattr(item, "evidence_id", None) for item in items]
            duplicate = (
                len(fingerprints) != len(items)
                or len(set(fingerprints)) != len(fingerprints)
                or len(set(evidence_ids)) != len(evidence_ids)
            )
            return _assessment(
                entry,
                DependencyMaturity.INVALID if duplicate else DependencyMaturity.CONFLICTING,
                ("DUPLICATE_DEPENDENCY_EVIDENCE" if duplicate else "CONFLICTING_DEPENDENCY_EVIDENCE",),
                now,
                fingerprints,
            )

        try:
            item = _canonical_evidence(items[0])
        except Exception:
            return _assessment(entry, DependencyMaturity.INVALID, ("DEPENDENCY_EVIDENCE_MALFORMED",), now)
        fingerprint = item.envelope_fingerprint
        codes: list[str] = []
        maturity_candidates = [DependencyMaturity.LOCAL_CONTRACT]

        if item.evidence_type not in entry.required_evidence_types:
            codes.append("DEPENDENCY_EVIDENCE_TYPE_MISMATCH")
        if item.subject_id != context.subject_id:
            codes.append("DEPENDENCY_SUBJECT_MISMATCH")
        if item.request_id != context.request_id:
            codes.append("DEPENDENCY_REQUEST_MISMATCH")
        if item.challenge_id != context.challenge_id:
            codes.append("DEPENDENCY_CHALLENGE_MISMATCH")
        if item.environment is not context.environment:
            codes.append("DEPENDENCY_ENVIRONMENT_MISMATCH")
        if item.scope_fingerprint != context.scope_fingerprint:
            codes.append("DEPENDENCY_SCOPE_MISMATCH")
        if item.acceptance_policy_version != context.acceptance_policy_version:
            codes.append("DEPENDENCY_POLICY_VERSION_MISMATCH")
        if entry.requires_commit_binding and item.commit_hash is None:
            codes.append("DEPENDENCY_COMMIT_BINDING_MISSING")
            maturity_candidates.append(DependencyMaturity.INVALID)
        elif item.commit_hash is not None and item.commit_hash != context.commit_hash:
            codes.append("DEPENDENCY_COMMIT_MISMATCH")
        if context.environment is IntegrationEnvironment.PRODUCTION:
            codes.append("PHASE21_PRODUCTION_ENVIRONMENT_UNAVAILABLE")
        if item.issued_at > now or (item.verified_at is not None and item.verified_at > now):
            codes.append("DEPENDENCY_EVIDENCE_FUTURE_DATED")
            maturity_candidates.append(DependencyMaturity.INVALID)
        if item.expires_at <= now or now - item.issued_at > timedelta(seconds=entry.freshness_seconds):
            codes.append("DEPENDENCY_EVIDENCE_STALE")
            maturity_candidates.append(DependencyMaturity.STALE)
        if entry.requires_monotonic_sequence and item.source_sequence is None:
            codes.append("DEPENDENCY_SEQUENCE_MISSING")
            maturity_candidates.append(DependencyMaturity.INVALID)
        if entry.requires_signature and (
            item.signature_fingerprint is None or item.trust_chain_fingerprint is None
        ):
            codes.append("DEPENDENCY_SIGNATURE_OR_TRUST_CHAIN_MISSING")
            maturity_candidates.append(DependencyMaturity.INVALID)
        if item.revocation_state in {
            EvidenceRevocationState.REVOKED,
            EvidenceRevocationState.SUPERSEDED,
            EvidenceRevocationState.COMPROMISED,
        }:
            codes.append("DEPENDENCY_IDENTITY_REVOKED")
            maturity_candidates.append(DependencyMaturity.REVOKED)
        elif item.revocation_state is EvidenceRevocationState.UNKNOWN:
            codes.append("DEPENDENCY_REVOCATION_UNKNOWN")
            maturity_candidates.append(DependencyMaturity.UNAVAILABLE)

        (
            payload_codes,
            payload_invalid,
            payload_stale,
            payload_timestamp_invalid,
        ) = _payload_codes(
            item,
            context,
            freshness_seconds=entry.freshness_seconds,
        )
        codes.extend(payload_codes)
        is_recovery = type(item.payload) is RecoveryRehearsalPayload
        if not is_recovery:
            if payload_invalid or payload_timestamp_invalid:
                maturity_candidates.append(DependencyMaturity.INVALID)
            if payload_stale:
                maturity_candidates.append(DependencyMaturity.STALE)

        if item.synthetic:
            codes.append("SYNTHETIC_EVIDENCE_NOT_OPERATIONAL")
        elif item.provenance is EvidenceProvenance.SANDBOX_PROVIDER:
            codes.append("DEPENDENCY_ISSUER_UNVERIFIED")
            codes.append("SANDBOX_EVIDENCE_NOT_INDEPENDENTLY_VERIFIED")
        elif item.provenance in {
            EvidenceProvenance.LOCAL_CONTRACT,
            EvidenceProvenance.MANUAL_CLAIM,
        }:
            codes.append("CALLER_EVIDENCE_NOT_AUTHORITATIVE")
        else:
            # An envelope can describe a claimed verification, but no independently
            # configured verifier exists in Phase 21 to authenticate that claim.
            codes.append("DEPENDENCY_ISSUER_UNVERIFIED")
            codes.append("INDEPENDENT_VERIFIER_NOT_CONFIGURED")

        if item.verification_result in {
            EvidenceVerificationResult.INVALID,
            EvidenceVerificationResult.UNAVAILABLE,
            EvidenceVerificationResult.NOT_VERIFIED,
        }:
            codes.append(
                {
                    EvidenceVerificationResult.INVALID: "CLAIMED_VERIFICATION_INVALID",
                    EvidenceVerificationResult.UNAVAILABLE: "CLAIMED_VERIFICATION_UNAVAILABLE",
                    EvidenceVerificationResult.NOT_VERIFIED: "DEPENDENCY_EVIDENCE_NOT_VERIFIED",
                }[item.verification_result]
            )
            maturity_candidates.append(
                DependencyMaturity.INVALID
                if item.verification_result is EvidenceVerificationResult.INVALID
                else DependencyMaturity.UNAVAILABLE
            )
        elif item.verification_result is EvidenceVerificationResult.SANDBOX_ONLY:
            codes.append("SANDBOX_VERIFICATION_NOT_PRODUCTION_ACCEPTANCE")
        elif item.verification_result is EvidenceVerificationResult.INDEPENDENTLY_VERIFIED:
            codes.append("CLAIMED_INDEPENDENT_VERIFICATION_NOT_TRUSTED_OFFLINE")

        if any(code.endswith("MISMATCH") or code.endswith("MALFORMED") for code in codes):
            maturity_candidates.append(DependencyMaturity.INVALID)
        maturity = _resolve_maturity(
            maturity_candidates,
            recovery_payload=is_recovery,
            payload_invalid=payload_invalid,
            payload_stale=payload_stale,
            payload_timestamp_invalid=payload_timestamp_invalid,
        )
        if item.synthetic and maturity is DependencyMaturity.LOCAL_CONTRACT:
            maturity = DependencyMaturity.SYNTHETIC_TESTED
        return _assessment(entry, maturity, tuple(codes), now, (fingerprint,))


def _entry(
    dependency_id: ExternalDependencyId,
    evidence_type: DependencyEvidenceType,
    purpose: str,
    boundary: str,
    owner: str,
    verification: str,
    failures: tuple[str, ...],
    criteria: tuple[str, ...],
    mode: VerificationMode,
    freshness: int,
    signature: bool,
    sequence: bool,
    commit_binding: bool = False,
) -> DependencyAcceptanceRegistryEntry:
    return DependencyAcceptanceRegistryEntry(
        dependency_id=dependency_id,
        purpose=purpose,
        security_boundary=boundary,
        required_evidence_types=(evidence_type,),
        owner_role=owner,
        independent_verification_method=verification,
        freshness_seconds=freshness,
        identity_scope_requirements=(
            "exact dependency and issuer identity",
            "exact environment and subject",
            "exact scope, challenge, policy, and release binding",
        ),
        expected_failure_modes=failures,
        acceptance_criteria=criteria,
        verification_mode=mode,
        blocker_codes=("REAL_INDEPENDENT_DEPENDENCY_UNAVAILABLE",),
        allowed_environments=tuple(IntegrationEnvironment),
        retention_and_redaction=(
            "Retain canonical fingerprints, bounded reason codes, issuer role, and timestamps; "
            "never retain credentials, raw signed payloads, broker responses, or account identifiers."
        ),
        requires_signature=signature,
        requires_monotonic_sequence=sequence,
        requires_commit_binding=commit_binding,
    )


def _payload_codes(
    item: ExternalDependencyEvidenceEnvelope,
    context: DependencyAcceptanceContext,
    *,
    freshness_seconds: int,
) -> tuple[tuple[str, ...], bool, bool, bool]:
    payload = item.payload
    codes: list[str] = []
    invalid = False
    stale = False
    timestamp_invalid = False

    if type(payload) is BrokerEvidencePayload:
        required = set(BrokerEvidenceField)
        if set(payload.required_fields) != required:
            codes.append("BROKER_EVIDENCE_INCOMPLETE")
            invalid = True
        if payload.account_scope_fingerprint != context.scope_fingerprint:
            codes.append("BROKER_ACCOUNT_SCOPE_MISMATCH")
            invalid = True
        if payload.unresolved_conflicts:
            codes.append("BROKER_EVIDENCE_CONFLICT")
            invalid = True
        if not payload.read_only_capability_claimed or payload.capability_evidence_fingerprint is None:
            codes.append("BROKER_READ_ONLY_CAPABILITY_MISSING")
            invalid = True
        else:
            codes.append("BROKER_READ_ONLY_CAPABILITY_UNVERIFIED")

    elif type(payload) is ReconciliationEvidencePayload:
        if (
            not payload.complete
            or payload.covered_record_count != payload.expected_record_count
        ):
            codes.append("RECONCILIATION_COVERAGE_INCOMPLETE")
            invalid = True
        if payload.unresolved_discrepancies:
            codes.append("RECONCILIATION_DISCREPANCY")
            invalid = True
        if payload.conflicting_generation:
            codes.append("RECONCILIATION_GENERATION_CONFLICT")
            invalid = True
        codes.append("RECONCILIATION_AUTHORITY_UNVERIFIED")

    elif type(payload) is MonitoringDeliveryPayload:
        if not (item.issued_at <= payload.event_at <= item.expires_at):
            codes.append("MONITORING_EVENT_CHRONOLOGY_INVALID")
            invalid = True
        if payload.outcome is not DeliveryOutcome.DELIVERED:
            codes.append("MONITORING_DELIVERY_NOT_CONFIRMED")
            invalid = True
        codes.append("MONITORING_DELIVERY_RECEIPT_UNVERIFIED")

    elif type(payload) is RecoveryRehearsalPayload:
        if (
            payload.completed_at is None
            or payload.observed_rpo_seconds is None
            or payload.observed_rto_seconds is None
            or payload.outcome is not RehearsalOutcome.PASSED
        ):
            codes.append("RECOVERY_REHEARSAL_INCOMPLETE")
            invalid = True
        elif (
            payload.observed_rpo_seconds > payload.maximum_rpo_seconds
            or payload.observed_rto_seconds > payload.maximum_rto_seconds
        ):
            codes.append("RECOVERY_OBJECTIVE_MISSED")
            invalid = True
        if (
            payload.replay_consistency is not ConsistencyOutcome.CONSISTENT
            or payload.witness_consistency is not ConsistencyOutcome.CONSISTENT
        ):
            codes.append("RECOVERY_REPLAY_WITNESS_INCONSISTENT")
            invalid = True
        if payload.started_at > context.evaluated_at or (
            payload.completed_at is not None
            and payload.completed_at > context.evaluated_at
        ):
            codes.append("RECOVERY_REHEARSAL_FUTURE_DATED")
            invalid = True
            timestamp_invalid = True
        if payload.completed_at is not None:
            if not (item.issued_at <= payload.completed_at <= item.expires_at):
                codes.append("RECOVERY_REHEARSAL_OUTSIDE_EVIDENCE_WINDOW")
                invalid = True
                timestamp_invalid = True
            if context.evaluated_at - payload.completed_at > timedelta(
                seconds=freshness_seconds
            ):
                codes.append("RECOVERY_REHEARSAL_STALE")
                stale = True
        codes.append("RECOVERY_REHEARSAL_UNVERIFIED")

    elif type(payload) is SecurityReviewPayload:
        if payload.target_commit != context.commit_hash:
            codes.append("SECURITY_REVIEW_COMMIT_MISMATCH")
            invalid = True
        if payload.review_scope_fingerprint != context.scope_fingerprint:
            codes.append("SECURITY_REVIEW_SCOPE_MISMATCH")
            invalid = True
        if payload.decision_expires_at <= context.evaluated_at:
            codes.append("SECURITY_REVIEW_EXPIRED")
            invalid = True
        if not payload.independence_attested:
            codes.append("SECURITY_REVIEW_NOT_INDEPENDENT")
            invalid = True
        if (
            payload.decision is not ReviewDecision.APPROVED
            or payload.unresolved_critical_count
            or payload.unresolved_high_count
            or payload.outstanding_blocker_count
        ):
            codes.append("SECURITY_REVIEW_BLOCKERS_UNRESOLVED")
            invalid = True
        codes.append("REVIEWER_INDEPENDENCE_UNVERIFIED")

    elif type(payload) is HumanApprovalPayload:
        if payload.target_commit != context.commit_hash:
            codes.append("APPROVAL_COMMIT_MISMATCH")
            invalid = True
        if payload.approved_scope_fingerprint != context.scope_fingerprint:
            codes.append("APPROVAL_SCOPE_MISMATCH")
            invalid = True
        if payload.policy_version != context.acceptance_policy_version:
            codes.append("APPROVAL_POLICY_MISMATCH")
            invalid = True
        if payload.issued_at != item.issued_at or payload.expires_at != item.expires_at:
            codes.append("APPROVAL_CHRONOLOGY_BINDING_MISMATCH")
            invalid = True
        if payload.expires_at <= context.evaluated_at:
            codes.append("APPROVAL_EXPIRED")
            invalid = True
        if payload.revocation_state is not EvidenceRevocationState.ACTIVE:
            codes.append("APPROVAL_REVOKED_OR_UNKNOWN")
            invalid = True
        if payload.decision is not ApprovalDecision.APPROVED:
            codes.append("APPROVAL_NOT_GRANTED")
            invalid = True
        codes.append("APPROVER_AUTHENTICATION_UNVERIFIED")

    return tuple(codes), invalid, stale, timestamp_invalid


def _canonical_context(context: DependencyAcceptanceContext) -> DependencyAcceptanceContext:
    """Reconstruct the exact context model instead of trusting copied instances."""

    if type(context) is not DependencyAcceptanceContext:
        raise ValueError("unexpected dependency acceptance context type")
    raw_state = object.__getattribute__(context, "__dict__")
    if type(raw_state) is not dict or any(type(key) is not str for key in raw_state):
        raise ValueError("dependency acceptance context state is malformed")
    values = dict(raw_state)
    expected_fields = set(DependencyAcceptanceContext.model_fields)
    actual_fields = set(values)
    if actual_fields != expected_fields:
        missing = tuple(sorted(expected_fields - actual_fields))
        undeclared = tuple(sorted(actual_fields - expected_fields))
        raise ValueError(
            "dependency acceptance context state fields do not match the model: "
            f"missing={missing}, undeclared={undeclared}"
        )
    try:
        extra_state = object.__getattribute__(context, "__pydantic_extra__")
    except AttributeError as error:
        raise ValueError("dependency acceptance context extra state is unavailable") from error
    if extra_state is not None and (type(extra_state) is not dict or bool(extra_state)):
        raise ValueError("dependency acceptance context contains undeclared extra state")
    return DependencyAcceptanceContext.model_validate(
        values,
        strict=True,
    )


def _resolve_maturity(
    candidates: list[DependencyMaturity],
    *,
    recovery_payload: bool,
    payload_invalid: bool,
    payload_stale: bool,
    payload_timestamp_invalid: bool,
) -> DependencyMaturity:
    """Apply explicit general precedence while retaining recovery semantics."""

    general = max(candidates, key=_GENERAL_MATURITY_PRECEDENCE.__getitem__)
    if not recovery_payload:
        return general

    # Recovery identity revocation remains terminal. Timestamp-invalid recovery
    # evidence cannot be softened by staleness. Otherwise an already invalid
    # envelope remains invalid, while an otherwise valid but old rehearsal is
    # stale even if it also has a non-temporal payload defect.
    if general is DependencyMaturity.REVOKED:
        return general
    if payload_timestamp_invalid:
        return DependencyMaturity.INVALID
    if general is DependencyMaturity.INVALID:
        return general
    if general is DependencyMaturity.STALE or payload_stale:
        return DependencyMaturity.STALE
    if payload_invalid:
        return DependencyMaturity.INVALID
    return general


def _canonical_evidence(
    item: ExternalDependencyEvidenceEnvelope,
) -> ExternalDependencyEvidenceEnvelope:
    if type(item) is not ExternalDependencyEvidenceEnvelope:
        raise ValueError("unexpected dependency evidence type")
    return ExternalDependencyEvidenceEnvelope.model_validate(
        item.model_dump(
            exclude={"envelope_fingerprint"},
            exclude_computed_fields=True,
        )
    )


def _safe_fingerprints(items: list[ExternalDependencyEvidenceEnvelope]) -> tuple[str, ...]:
    fingerprints: list[str] = []
    for item in items:
        try:
            fingerprints.append(_canonical_evidence(item).envelope_fingerprint)
        except Exception:
            continue
    return tuple(sorted(fingerprints))


def _assessment(
    entry: DependencyAcceptanceRegistryEntry,
    maturity: DependencyMaturity,
    reasons: tuple[str, ...],
    evaluated_at,
    fingerprints: tuple[str, ...] = (),
) -> DependencyAcceptanceAssessment:
    return DependencyAcceptanceAssessment(
        dependency_id=entry.dependency_id,
        maturity=maturity,
        reason_codes=tuple(sorted(set(reasons))),
        evidence_fingerprints=fingerprints,
        evaluated_at=evaluated_at,
    )
