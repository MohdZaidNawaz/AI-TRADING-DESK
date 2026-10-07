"""Hard-gated Pilot coordinator that remains disabled in Phase 3."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.models.proposals import DisabledPilotResult, SentinelDecision, TradeProposal
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry
from app.execution.intent_store import ExecutionIntentStore
from app.execution.adapter import ExecutionAdapter
from app.models.execution import ExecutionIntent, ExecutionResult, ExecutionStatus
from app.services.execution_validation import validate

if TYPE_CHECKING:
    from app.services.execution_gate import FinalExecutionGate


class PilotApprovalError(ValueError):
    """Raised before Pilot can accept an invalid or unavailable approval."""


class PilotExecutionCoordinator:
    """Validate a Sentinel approval, consume it once, and perform no execution."""

    def __init__(self, approval_registry: ApprovalRegistry, intent_store: ExecutionIntentStore | None = None, adapter: ExecutionAdapter | None = None, safety_gate: "FinalExecutionGate | None" = None) -> None:
        self._approval_registry = approval_registry
        self._intent_store = intent_store or ExecutionIntentStore()
        self._adapter = adapter
        self._safety_gate = safety_gate

    def prepare_execution(self, *, proposal: TradeProposal, decision: SentinelDecision, now: datetime) -> tuple[ExecutionIntent | None, ExecutionResult]:
        """Validate an approved proposal without consuming approval or calling an adapter.

        Approval consumption is deliberately reserved for future adapter submission;
        Phase 8 only creates a bounded, immutable intent and stops unavailable.
        """
        reasons = validate(proposal, decision, now)
        if not reasons:
            try:
                self._approval_registry.verify(proposal=proposal, decision=decision, now=now)
            except ApprovalClaimError:
                reasons = ("APPROVAL_NOT_FOUND",)
        if reasons:
            return None, ExecutionResult(intent_id="", proposal_id=proposal.proposal_id, status=ExecutionStatus.NOT_EXECUTED, reason_codes=reasons, evaluated_at=now, adapter_status="ADAPTER_NOT_CONNECTED")
        params = proposal.parameters
        assert params is not None and params.direction is not None and params.reference_price is not None and params.stop_loss is not None and params.targets
        assert params.quantity is not None and params.order_type is not None and params.filling_mode is not None
        intent = ExecutionIntent(proposal_id=proposal.proposal_id, proposal_fingerprint=self._proposal_fingerprint(proposal), analysis_id=proposal.analysis_id, symbol=proposal.symbol or "", asset_class=proposal.asset_class, direction=params.direction, requested_entry=params.reference_price, stop_loss=params.stop_loss, take_profit=params.targets[0], requested_risk_fraction=params.requested_risk_fraction, quantity=params.quantity, order_type=params.order_type, filling_mode=params.filling_mode, time_in_force=params.time_in_force, approval_id=decision.approval_id or "", policy_id=decision.policy_id, policy_fingerprint=decision.policy_fingerprint, assessment_id=decision.assessment_id, assessment_fingerprint=decision.assessment_fingerprint, evidence_fingerprint=proposal.evidence_fingerprint, account_id=decision.account_id, portfolio_snapshot_id=decision.portfolio_snapshot_id, portfolio_snapshot_fingerprint=decision.portfolio_snapshot_fingerprint, created_at=now, expires_at=decision.expires_at or now, status=ExecutionStatus.READY_FOR_ADAPTER)
        intent=self._intent_store.put(intent)
        if self._adapter is None:
            return intent, ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=proposal.proposal_id, status=ExecutionStatus.ADAPTER_UNAVAILABLE, reason_codes=("ADAPTER_NOT_AVAILABLE",), evaluated_at=now, adapter_status="ADAPTER_NOT_CONNECTED")
        if self._safety_gate is None:
            return intent, ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=proposal.proposal_id, status=ExecutionStatus.NOT_EXECUTED, reason_codes=("FINAL_SAFETY_GATE_REQUIRED",), evaluated_at=now, adapter_status="FINAL_GATE_REQUIRED")
        mode = getattr(self._adapter, "execution_mode", "disabled")
        execution_mode = getattr(mode, "value", str(mode))
        safety = self._safety_gate.evaluate_submission(proposal=proposal, decision=decision, intent=intent, execution_mode=execution_mode, now=now)
        if safety.adapter_result is not None:
            return intent, safety.adapter_result
        return intent, ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=proposal.proposal_id, status=ExecutionStatus.NOT_EXECUTED, reason_codes=safety.reason_codes or ("EXECUTION_DISABLED",), evaluated_at=now, adapter_status=f"FINAL_GATE_{safety.execution.state.value.upper()}")

    @staticmethod
    def _proposal_fingerprint(proposal: TradeProposal) -> str:
        from app.services.trade_proposal_workflow import proposal_fingerprint
        return proposal_fingerprint(proposal)

    def prepare(
        self,
        *,
        proposal: TradeProposal,
        decision: SentinelDecision,
        now: datetime,
    ) -> DisabledPilotResult:
        """Accept only a registry-verified approval and always remain disabled."""
        try:
            verified = self._approval_registry.claim(
                proposal=proposal,
                decision=decision,
                now=now,
            )
        except ApprovalClaimError as exc:
            raise PilotApprovalError(str(exc)) from None

        return DisabledPilotResult(
            proposal_id=verified.proposal_id,
            approval_id=verified.approval_id,
            message="Pilot is disabled in Phase 3; no execution adapter was invoked.",
        )
