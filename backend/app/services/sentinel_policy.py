"""Construction of Phase 7A policy and non-authorizing assessment artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json

from app.models.proposals import EvidenceReference, RiskContext, TradeProposal
from app.models.risk_assessment import (
    RiskCheckInputReference,
    RiskCheckResult,
    RiskCheckStatus,
    SentinelRiskAssessment,
    SentinelAccountBinding,
    SentinelEventRiskAssessment,
)
from app.models.sentinel_policy import (
    MissingDataStrategy,
    SentinelPolicyMetadata,
    SentinelRiskPolicy,
)


PHASE7_POLICY_ID = "sentinel-risk-policy"
PHASE7_POLICY_VERSION = "phase7a-deterministic-v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def policy_from_risk_context(risk_context: RiskContext | None) -> SentinelRiskPolicy:
    """Capture existing Gate configuration as one immutable policy artifact.

    This is deliberately a representation of the legacy settings only; it does
    not consume portfolio metrics or add any new risk threshold.
    """
    return SentinelRiskPolicy(
        policy_id=PHASE7_POLICY_ID,
        policy_version=PHASE7_POLICY_VERSION,
        effective_at=_EPOCH,
        missing_data_strategy=MissingDataStrategy.INSUFFICIENT_DATA,
        max_market_evidence_age_seconds=(
            risk_context.max_market_evidence_age_seconds if risk_context is not None else None
        ),
        approval_ttl_seconds=(risk_context.approval_ttl_seconds if risk_context is not None else None),
        max_requested_risk_fraction=(
            risk_context.max_risk_fraction if risk_context is not None else None
        ),
        required_agreeing_specialists=(
            risk_context.required_agreeing_specialists if risk_context is not None else None
        ),
        reject_conflicting_signals=(
            risk_context.reject_conflicting_signals if risk_context is not None else None
        ),
        metadata=(SentinelPolicyMetadata(key="policy_scope", value="existing_sentinel_rules"),),
    )


def policy_effectiveness_check(
    policy: SentinelRiskPolicy,
    *,
    evaluated_at: datetime,
) -> RiskCheckResult:
    """Produce a factual effective-interval result without granting approval."""
    reference = _policy_reference(policy)
    if policy.is_effective_at(evaluated_at):
        return RiskCheckResult(
            check_name="policy_effective_time",
            status=RiskCheckStatus.PASS,
            reason_codes=("policy_effective",),
            reason="The immutable Sentinel policy is effective at evaluation time.",
            input_references=(reference,),
        )
    reason = "policy_not_effective" if evaluated_at < policy.effective_at else "policy_expired"
    return RiskCheckResult(
        check_name="policy_effective_time",
        status=RiskCheckStatus.REJECTED,
        reason_codes=(reason,),
        reason="The immutable Sentinel policy is not effective at evaluation time.",
        input_references=(reference,),
    )


def build_risk_assessment(
    *,
    policy: SentinelRiskPolicy,
    proposal: TradeProposal,
    proposal_fingerprint: str,
    evidence_fingerprint: str,
    evidence_references: tuple[EvidenceReference, ...],
    evaluated_at: datetime,
    risk_context: RiskContext | None,
    snapshot_rejections: tuple[str, ...],
    parameter_rejections: tuple[str, ...],
    symbol_rejections: tuple[str, ...],
    risk_limit_rejections: tuple[str, ...],
    signal_rejections: tuple[str, ...],
    missing_requirements: tuple[str, ...],
    additional_checks: tuple[RiskCheckResult, ...] = (),
    account_binding: SentinelAccountBinding | None = None,
    event_assessments: tuple[SentinelEventRiskAssessment, ...] = (),
) -> SentinelRiskAssessment:
    """Convert existing Gate outcomes into immutable deterministic check facts."""
    base_references: tuple[RiskCheckInputReference, ...] = (
        RiskCheckInputReference(
            reference_type="proposal",
            reference_id=proposal.proposal_id,
            fingerprint=proposal_fingerprint,
        ),
        RiskCheckInputReference(
            reference_type="evidence_bundle",
            reference_id="captured_snapshot",
            fingerprint=evidence_fingerprint,
        ),
        _policy_reference(policy),
    )
    if risk_context is not None:
        base_references = (*base_references, _risk_context_reference(risk_context))
    evidence_inputs = tuple(
        RiskCheckInputReference(
            reference_type="evidence",
            reference_id=f"{item.source_id}:{item.event_id}",
        )
        for item in evidence_references
    )
    checks = (
        policy_effectiveness_check(policy, evaluated_at=evaluated_at),
        _result("snapshot_provenance", snapshot_rejections, base_references),
        _result("proposal_parameters", parameter_rejections, base_references),
        _result("symbol_alignment", symbol_rejections, base_references),
        _result("risk_limits", risk_limit_rejections, base_references),
        _result("specialist_conflicts", signal_rejections, base_references),
        _result(
            "required_information",
            missing_requirements,
            (*base_references, *evidence_inputs),
            missing=True,
        ),
        *additional_checks,
    )
    return SentinelRiskAssessment(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        proposal_id=proposal.proposal_id,
        proposal_fingerprint=proposal_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
        evidence_references=evidence_inputs,
        account_binding=account_binding,
        event_assessments=event_assessments,
        checks=checks,
        evaluated_at=evaluated_at,
        overall_status=_overall_from_checks(checks),
    )


def _result(
    check_name: str,
    reason_codes: tuple[str, ...],
    input_references: tuple[RiskCheckInputReference, ...],
    *,
    missing: bool = False,
) -> RiskCheckResult:
    if reason_codes:
        status = RiskCheckStatus.INSUFFICIENT_DATA if missing else RiskCheckStatus.REJECTED
        reason = "Required information is unavailable." if missing else "Existing Sentinel validation failed."
    else:
        status = RiskCheckStatus.PASS
        reason = "Existing Sentinel validation passed."
    return RiskCheckResult(
        check_name=check_name,
        status=status,
        reason_codes=reason_codes,
        reason=reason,
        input_references=input_references,
    )


def _overall_from_checks(checks: tuple[RiskCheckResult, ...]) -> RiskCheckStatus:
    if any(item.status is RiskCheckStatus.REJECTED for item in checks):
        return RiskCheckStatus.REJECTED
    if any(item.status is RiskCheckStatus.INSUFFICIENT_DATA for item in checks):
        return RiskCheckStatus.INSUFFICIENT_DATA
    return RiskCheckStatus.PASS


def _policy_reference(policy: SentinelRiskPolicy) -> RiskCheckInputReference:
    return RiskCheckInputReference(
        reference_type="sentinel_policy",
        reference_id=policy.policy_id,
        fingerprint=policy.fingerprint,
    )


def _risk_context_reference(risk_context: RiskContext) -> RiskCheckInputReference:
    """Bind legacy caller-supplied context without exposing mutable runtime state."""
    encoded = json.dumps(
        risk_context.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return RiskCheckInputReference(
        reference_type="legacy_risk_context",
        reference_id="explicit_risk_context",
        fingerprint=sha256(encoded.encode("utf-8")).hexdigest(),
    )
