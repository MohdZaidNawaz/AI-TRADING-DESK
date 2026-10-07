"""Pure, fail-closed controlled-demo readiness evaluation; no adapter calls."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from numbers import Real

from app.audit.models import AuditEvent, AuditEventType
from app.audit.service import AuditService
from app.audit.store import AuditStoreError
from app.execution.kill_switch import ExecutionKillSwitch
from app.models.demo_execution import (
    ControlledDemoExecutionPolicy,
    DemoReadinessAssessment,
    DemoReadinessContext,
)
from app.models.execution import ExecutionIntent
from app.models.execution_state import ExecutionLifecycleState
from app.models.proposals import SentinelDecision, TradeDirection, TradeProposal
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry, proposal_fingerprint


class ControlledDemoReadinessGate:
    """Assess a future demo path without authorizing or invoking execution."""

    def __init__(self, *, approval_registry: ApprovalRegistry, kill_switch: ExecutionKillSwitch, audit_service: AuditService | None = None) -> None:
        self._approval_registry = approval_registry
        self._kill_switch = kill_switch
        self._audit_service = audit_service

    def evaluate(
        self,
        *,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal,
        decision: SentinelDecision,
        intent: ExecutionIntent,
        context: DemoReadinessContext,
        now: datetime,
    ) -> DemoReadinessAssessment:
        codes = self._codes(policy=policy, proposal=proposal, decision=decision, intent=intent, context=context, now=now)
        assessment = DemoReadinessAssessment(
            policy_id=policy.policy_id,
            policy_fingerprint=policy.fingerprint,
            execution_intent_id=intent.execution_intent_id,
            execution_intent_fingerprint=intent.fingerprint,
            evaluated_at=now,
            reason_codes=tuple(sorted(set(codes))),
            demo_execution_enabled=policy.enabled,
            # Stage 3 is deliberately non-authorizing even if a future policy
            # object is constructed with enabled=True.
            submission_allowed=False,
        )
        if not self._audit(assessment=assessment, intent=intent, context=context):
            return assessment.model_copy(
                update={"reason_codes": tuple(sorted(set((*assessment.reason_codes, "AUDIT_PERSISTENCE_UNAVAILABLE"))))}
            )
        return assessment

    def _codes(self, *, policy: ControlledDemoExecutionPolicy, proposal: TradeProposal, decision: SentinelDecision, intent: ExecutionIntent, context: DemoReadinessContext, now: datetime) -> list[str]:
        codes: list[str] = ["DEMO_CONTROLLED_DISABLED"] if not policy.enabled else []
        if self._kill_switch.status().active:
            codes.append("KILL_SWITCH_ACTIVE")
        if now >= intent.expires_at:
            codes.append("INTENT_EXPIRED")
        if context.account.account != policy.allowed_account or intent.account_id != policy.allowed_account.account_id:
            codes.append("DEMO_ACCOUNT_MISMATCH")
        if intent.symbol != policy.instrument.canonical_symbol or context.instrument.canonical_symbol != policy.instrument.canonical_symbol or context.instrument.broker_symbol != policy.instrument.broker_symbol:
            codes.append("DEMO_SYMBOL_MISMATCH")
        if intent.order_type not in policy.allowed_order_types or intent.order_type not in context.instrument.order_modes:
            codes.append("DEMO_UNSUPPORTED_ORDER_TYPE")
        if intent.filling_mode not in policy.allowed_filling_modes or intent.filling_mode not in context.instrument.filling_modes:
            codes.append("DEMO_UNSUPPORTED_FILLING_MODE")
        if intent.filling_mode == "BOOK_OR_CANCEL":
            codes.append("DEMO_UNSUPPORTED_FILLING_MODE")
        if intent.quantity is None or intent.quantity <= 0 or intent.quantity > policy.max_order_volume:
            codes.append("DEMO_VOLUME_LIMIT")
        if len(context.positions) + 1 > policy.max_open_positions:
            codes.append("DEMO_POSITION_COUNT_LIMIT")
        if sum(position.volume for position in context.positions) + (intent.quantity or 0) > policy.max_total_open_volume:
            codes.append("DEMO_TOTAL_OPEN_VOLUME_LIMIT")
        if now - context.market_observed_at > timedelta(seconds=policy.max_market_age_seconds) or context.market_observed_at > now:
            codes.append("DEMO_MARKET_DATA_STALE")
        codes.extend(_price_codes(intent=intent, policy=policy))
        codes.extend(_financial_codes(intent=intent, policy=policy, context=context))
        cutoff = now - timedelta(seconds=policy.controlled_window_seconds)
        recent = [record for record in context.execution_history if record.account_id == policy.allowed_account.account_id and record.created_at >= cutoff]
        if any(record.execution_intent_id == intent.execution_intent_id for record in recent):
            codes.append("DEMO_DUPLICATE_EXECUTION")
        if len(recent) >= policy.max_executions_in_window:
            codes.append("DEMO_EXECUTION_WINDOW_LIMIT")
        if proposal_fingerprint(proposal) != intent.proposal_fingerprint:
            codes.append("DEMO_INTENT_PROPOSAL_MISMATCH")
        try:
            self._approval_registry.verify(proposal=proposal, decision=decision, now=now)
        except ApprovalClaimError:
            codes.append("DEMO_SENTINEL_APPROVAL_INVALID")
        return codes

    def _audit(self, *, assessment: DemoReadinessAssessment, intent: ExecutionIntent, context: DemoReadinessContext) -> bool:
        if self._audit_service is None:
            return True
        try:
            self._audit_service.record(AuditEvent(
                event_type=AuditEventType.EXECUTION_LIFECYCLE,
                occurred_at=assessment.evaluated_at,
                intent_id=intent.execution_intent_id,
                proposal_id=intent.proposal_id,
                approval_id=intent.approval_id,
                account_reference=context.account.account.account_reference,
                state="demo_readiness_blocked",
                reason_code=assessment.reason_codes[0] if assessment.reason_codes else "DEMO_READINESS_EVALUATED",
                provenance_fingerprint=assessment.policy_fingerprint,
                details=(("demo_execution_enabled", str(assessment.demo_execution_enabled).lower()), ("submission_allowed", "false")),
            ))
        except AuditStoreError:
            return False
        return True


def _price_codes(*, intent: ExecutionIntent, policy: ControlledDemoExecutionPolicy) -> list[str]:
    values = (intent.requested_entry, intent.stop_loss, intent.take_profit)
    if not all(isinstance(value, Real) and isfinite(value) for value in values):
        return ["DEMO_INVALID_PRICE"]
    if abs(intent.requested_entry - intent.stop_loss) > policy.max_stop_distance:
        return ["DEMO_STOP_DISTANCE_LIMIT"]
    if intent.direction is TradeDirection.LONG:
        return ["DEMO_INVALID_STOP_DIRECTION"] if intent.stop_loss >= intent.requested_entry else (["DEMO_INVALID_TARGET"] if intent.take_profit <= intent.requested_entry else [])
    return ["DEMO_INVALID_STOP_DIRECTION"] if intent.stop_loss <= intent.requested_entry else (["DEMO_INVALID_TARGET"] if intent.take_profit >= intent.requested_entry else [])


def _financial_codes(*, intent: ExecutionIntent, policy: ControlledDemoExecutionPolicy, context: DemoReadinessContext) -> list[str]:
    instrument = context.instrument
    if (not isinstance(intent.requested_entry, Real) or not isinstance(intent.stop_loss, Real)
            or context.account.currency.upper() != policy.risk_currency or instrument.tick_size <= 0
            or instrument.tick_value is None or instrument.tick_value <= 0
            or instrument.contract_size is None or instrument.contract_size <= 0
            or intent.quantity is None):
        return ["DEMO_RISK_INPUTS_UNAVAILABLE"]
    candidate_risk = abs(intent.requested_entry - intent.stop_loss) / instrument.tick_size * instrument.tick_value * intent.quantity
    notional = abs(intent.requested_entry) * intent.quantity * instrument.contract_size
    codes: list[str] = []
    if candidate_risk > policy.max_monetary_risk_per_execution:
        codes.append("DEMO_MONETARY_RISK_LIMIT")
    if notional > policy.max_order_notional:
        codes.append("DEMO_NOTIONAL_LIMIT")
    existing_risk = 0.0
    for position in context.positions:
        if position.symbol != policy.instrument.broker_symbol or position.stop_loss is None:
            return [*codes, "DEMO_EXISTING_POSITION_RISK_UNAVAILABLE"]
        existing_risk += abs(position.price_open - position.stop_loss) / instrument.tick_size * instrument.tick_value * position.volume
    if existing_risk + candidate_risk > policy.max_aggregate_monetary_risk:
        codes.append("DEMO_AGGREGATE_MONETARY_RISK_LIMIT")
    return codes
