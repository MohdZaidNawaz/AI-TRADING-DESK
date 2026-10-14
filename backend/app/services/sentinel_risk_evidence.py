"""Recompute controlled-demo policy risk from validated source observations.

This service does not issue or consume Sentinel approvals.  It records whether
the existing policy's numerical constraints can be independently recomputed
from a validated evidence bundle and exact proposal/intent lineage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.execution import ExecutionIntent
from app.models.proposals import SentinelDecision, SentinelDecisionStatus, TradeDirection, TradeProposal
from app.models.sentinel_risk_evidence import SentinelPortfolioRiskEvidence, SentinelRiskEvidenceStatus
from app.models.sentinel_policy_authority import SentinelPolicyResolutionStatus
from app.models.trusted_execution_evidence import ValidatedExecutionEvidence
from app.services.sentinel_policy_authority import (
    CONTROLLED_DEMO_RISK_CALCULATION_VERSION,
    SentinelPortfolioRiskPolicyVerifier,
)
from app.services.trade_proposal_workflow import proposal_fingerprint


class SentinelPortfolioRiskEvidenceVerifier:
    """Recompute risk only after exact policy source verification."""

    def __init__(
        self,
        policy_authority: SentinelPortfolioRiskPolicyVerifier | None = None,
    ) -> None:
        # Production composition is deliberately unavailable until an
        # independently approved policy source exists.
        self._policy_authority = policy_authority or SentinelPortfolioRiskPolicyVerifier()

    def verify(
        self,
        *,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal,
        decision: SentinelDecision,
        intent: ExecutionIntent,
        evidence: ValidatedExecutionEvidence,
        now: datetime,
    ) -> SentinelPortfolioRiskEvidence:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Sentinel risk evidence verification requires timezone awareness")
        now = now.astimezone(UTC)
        policy_resolution = self._policy_authority.resolve(
            candidate=policy,
            evaluated_at=now,
        )
        if policy_resolution.status not in {
            SentinelPolicyResolutionStatus.AUTHORITATIVE,
            SentinelPolicyResolutionStatus.TEST_ONLY_VALIDATED,
        }:
            return _unavailable_policy_result(
                policy=policy,
                proposal=proposal,
                decision=decision,
                intent=intent,
                evidence=evidence,
                now=now,
                source_id=policy_resolution.source_id,
                reason_codes=policy_resolution.reason_codes,
            )
        record = policy_resolution.record
        assert record is not None and record.approval is not None
        # Use only source-resolved canonical content for all calculations.  The
        # candidate is comparison input and cannot override limits.
        policy = record.policy
        codes: list[str] = []
        if not evidence.ready:
            codes.append("SENTINEL_RISK_EVIDENCE_INPUT_UNAVAILABLE")
        if decision.decision is not SentinelDecisionStatus.APPROVED or not decision.approval_id:
            codes.append("SENTINEL_RISK_EVIDENCE_APPROVAL_INVALID")
        if decision.expires_at is None or now >= decision.expires_at:
            codes.append("SENTINEL_RISK_EVIDENCE_APPROVAL_EXPIRED")
        if proposal_fingerprint(proposal) != decision.proposal_fingerprint or proposal_fingerprint(proposal) != intent.proposal_fingerprint:
            codes.append("SENTINEL_RISK_EVIDENCE_PROPOSAL_MISMATCH")
        if (
            decision.analysis_id != proposal.analysis_id
            or decision.evidence_fingerprint != proposal.evidence_fingerprint
            or intent.analysis_id != proposal.analysis_id
            or intent.evidence_fingerprint != proposal.evidence_fingerprint
        ):
            codes.append("SENTINEL_RISK_EVIDENCE_LINEAGE_MISMATCH")
        # The execution intent is bound to the Sentinel assessment policy,
        # while this verifier applies the distinct controlled-demo policy. Its
        # source-bound ``PortfolioRiskInputObservation`` carries the latter's
        # ID/fingerprint and is checked below.

        account = evidence.account
        instrument = evidence.instrument
        risk_input = evidence.portfolio_risk_inputs
        if account is None or instrument is None or risk_input is None:
            codes.append("SENTINEL_RISK_EVIDENCE_INPUT_UNAVAILABLE")
        else:
            if account.account != policy.allowed_account or intent.account_id != policy.allowed_account.account_id:
                codes.append("SENTINEL_RISK_EVIDENCE_ACCOUNT_MISMATCH")
            if (
                instrument.canonical_symbol != policy.instrument.canonical_symbol
                or instrument.broker_symbol != policy.instrument.broker_symbol
                or intent.symbol != policy.instrument.canonical_symbol
            ):
                codes.append("SENTINEL_RISK_EVIDENCE_INSTRUMENT_MISMATCH")
            if account.currency.upper() != policy.risk_currency:
                codes.append("SENTINEL_RISK_EVIDENCE_CURRENCY_MISMATCH")
            if risk_input.policy_id != policy.policy_id or risk_input.policy_fingerprint != policy.fingerprint:
                codes.append("SENTINEL_RISK_EVIDENCE_POLICY_MISMATCH")

        metrics = _risk_metrics(intent=intent, policy=policy, evidence=evidence)
        codes.extend(metrics.codes)
        status = (
            SentinelRiskEvidenceStatus.VALIDATED
            if not codes
            else SentinelRiskEvidenceStatus.UNAVAILABLE
            if "SENTINEL_RISK_EVIDENCE_INPUT_UNAVAILABLE" in codes
            else SentinelRiskEvidenceStatus.REJECTED
        )
        return SentinelPortfolioRiskEvidence(
            status=status,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_fingerprint=policy.fingerprint,
            policy_source_id=policy_resolution.source_id,
            policy_authority_level=policy_resolution.authority_level,
            policy_approval_reference=record.approval.artifact.approval_artifact_id,
            policy_approval_fingerprint=record.approval.artifact.policy_fingerprint,
            policy_approval_artifact_fingerprint=record.approval.artifact.canonical_payload_fingerprint,
            policy_approver_id=record.approval.artifact.approver_id,
            calculation_version=CONTROLLED_DEMO_RISK_CALCULATION_VERSION,
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal_fingerprint(proposal),
            analysis_id=proposal.analysis_id,
            evidence_fingerprint=proposal.evidence_fingerprint,
            decision_id=decision.decision_id,
            approval_id=decision.approval_id or "unavailable-approval",
            execution_intent_id=intent.execution_intent_id,
            execution_intent_fingerprint=intent.fingerprint,
            account_id=intent.account_id or "unavailable-account",
            canonical_symbol=intent.symbol,
            broker_symbol=policy.instrument.broker_symbol,
            risk_currency=policy.risk_currency,
            trusted_evidence_fingerprint=evidence.fingerprint,
            position_snapshot_fingerprint=None if risk_input is None else risk_input.position_snapshot_fingerprint,
            candidate_monetary_risk=metrics.candidate,
            existing_monetary_risk=metrics.existing,
            order_notional=metrics.notional,
            current_open_volume=metrics.current_volume,
            evaluated_at=now,
            reason_codes=tuple(sorted(set(codes))),
        )


def _unavailable_policy_result(
    *,
    policy: ControlledDemoExecutionPolicy,
    proposal: TradeProposal,
    decision: SentinelDecision,
    intent: ExecutionIntent,
    evidence: ValidatedExecutionEvidence,
    now: datetime,
    source_id: str,
    reason_codes: tuple[str, ...],
) -> SentinelPortfolioRiskEvidence:
    codes = tuple(reason_codes or ("SENTINEL_RISK_POLICY_AUTHORITY_UNAVAILABLE",))
    return SentinelPortfolioRiskEvidence(
        status=SentinelRiskEvidenceStatus.UNAVAILABLE,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        policy_source_id=source_id,
        calculation_version=CONTROLLED_DEMO_RISK_CALCULATION_VERSION,
        proposal_id=proposal.proposal_id,
        proposal_fingerprint=proposal_fingerprint(proposal),
        analysis_id=proposal.analysis_id,
        evidence_fingerprint=proposal.evidence_fingerprint,
        decision_id=decision.decision_id,
        approval_id=decision.approval_id or "unavailable-approval",
        execution_intent_id=intent.execution_intent_id,
        execution_intent_fingerprint=intent.fingerprint,
        account_id=intent.account_id or "unavailable-account",
        canonical_symbol=intent.symbol,
        broker_symbol=policy.instrument.broker_symbol,
        risk_currency=policy.risk_currency,
        trusted_evidence_fingerprint=evidence.fingerprint,
        position_snapshot_fingerprint=(
            None
            if evidence.portfolio_risk_inputs is None
            else evidence.portfolio_risk_inputs.position_snapshot_fingerprint
        ),
        evaluated_at=now,
        reason_codes=codes,
    )


class _Metrics:
    def __init__(self) -> None:
        self.codes: list[str] = []
        self.candidate: Decimal | None = None
        self.existing: Decimal | None = None
        self.notional: Decimal | None = None
        self.current_volume: Decimal | None = None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _risk_metrics(
    *, intent: ExecutionIntent, policy: ControlledDemoExecutionPolicy, evidence: ValidatedExecutionEvidence
) -> _Metrics:
    result = _Metrics()
    account, instrument = evidence.account, evidence.instrument
    values = (
        _decimal(intent.requested_entry), _decimal(intent.stop_loss), _decimal(intent.take_profit),
        _decimal(intent.quantity), None if instrument is None else _decimal(instrument.tick_size),
        None if instrument is None else _decimal(instrument.tick_value),
        None if instrument is None else _decimal(instrument.contract_size),
    )
    if account is None or instrument is None or any(value is None for value in values):
        result.codes.append("SENTINEL_RISK_EVIDENCE_INPUT_UNAVAILABLE")
        return result
    entry, stop, target, quantity, tick_size, tick_value, contract_size = values
    assert entry is not None and stop is not None and target is not None and quantity is not None
    assert tick_size is not None and tick_value is not None and contract_size is not None
    if quantity <= 0 or tick_size <= 0 or tick_value <= 0 or contract_size <= 0:
        result.codes.append("SENTINEL_RISK_EVIDENCE_INPUT_UNAVAILABLE")
        return result
    if (intent.direction is TradeDirection.LONG and (stop >= entry or target <= entry)) or (
        intent.direction is TradeDirection.SHORT and (stop <= entry or target >= entry)
    ):
        result.codes.append("SENTINEL_RISK_EVIDENCE_PRICE_DIRECTION_INVALID")
    if abs(entry - stop) > _decimal(policy.max_stop_distance):
        result.codes.append("DEMO_STOP_DISTANCE_LIMIT")
    result.candidate = abs(entry - stop) / tick_size * tick_value * quantity
    result.notional = abs(entry) * quantity * contract_size
    result.current_volume = sum((_decimal(item.volume) or Decimal("0") for item in evidence.positions), Decimal("0"))
    result.existing = Decimal("0")
    for position in evidence.positions:
        open_price, position_stop, volume = _decimal(position.price_open), _decimal(position.stop_loss), _decimal(position.volume)
        if position.symbol != policy.instrument.broker_symbol or position_stop is None or open_price is None or volume is None:
            result.codes.append("DEMO_EXISTING_POSITION_RISK_UNAVAILABLE")
            continue
        result.existing += abs(open_price - position_stop) / tick_size * tick_value * volume
    if quantity > _decimal(policy.max_order_volume):
        result.codes.append("DEMO_VOLUME_LIMIT")
    if len(evidence.positions) + 1 > policy.max_open_positions:
        result.codes.append("DEMO_POSITION_COUNT_LIMIT")
    if result.current_volume + quantity > _decimal(policy.max_total_open_volume):
        result.codes.append("DEMO_TOTAL_OPEN_VOLUME_LIMIT")
    if result.candidate > _decimal(policy.max_monetary_risk_per_execution):
        result.codes.append("DEMO_MONETARY_RISK_LIMIT")
    if result.notional > _decimal(policy.max_order_notional):
        result.codes.append("DEMO_NOTIONAL_LIMIT")
    if result.existing + result.candidate > _decimal(policy.max_aggregate_monetary_risk):
        result.codes.append("DEMO_AGGREGATE_MONETARY_RISK_LIMIT")
    return result
