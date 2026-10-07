"""Final fail-closed execution authorization boundary for Phase 10."""

from __future__ import annotations

from datetime import UTC, datetime

from app.execution.adapter import ExecutionAdapter
from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.state_store import ExecutionStateStore
from app.models.execution import ExecutionIntent, ExecutionStatus
from app.models.execution_state import (
    ExecutionLifecycleState,
    ExecutionSafetyDecision,
    build_execution_record,
)
from app.models.proposals import SentinelDecision, TradeProposal
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry, proposal_fingerprint


class FinalExecutionGate:
    """Controls broker submission; Phase 10 intentionally never authorizes live submit."""

    def __init__(
        self,
        *,
        approval_registry: ApprovalRegistry,
        state_store: ExecutionStateStore,
        kill_switch: ExecutionKillSwitch,
        adapter: ExecutionAdapter | None,
    ) -> None:
        self._approval_registry = approval_registry
        self._state_store = state_store
        self._kill_switch = kill_switch
        self._adapter = adapter

    def register_intent(self, *, intent: ExecutionIntent, execution_mode: str, now: datetime) -> ExecutionSafetyDecision:
        record = build_execution_record(intent, execution_mode=execution_mode, now=now)
        try:
            record, created = self._state_store.create_or_get(record)
        except ValueError:
            return ExecutionSafetyDecision(execution=build_execution_record(intent, execution_mode=execution_mode, now=now), reason_codes=("CONFLICTING_EXECUTION",))
        except RuntimeError:
            return ExecutionSafetyDecision(execution=build_execution_record(intent, execution_mode=execution_mode, now=now), reason_codes=("EXECUTION_STORE_CAPACITY",))
        if created:
            record = self._state_store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.VALIDATED, now=now)
            record = self._state_store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.READY_FOR_SUBMISSION, now=now)
        return ExecutionSafetyDecision(execution=record)

    def evaluate_submission(
        self,
        *,
        proposal: TradeProposal,
        decision: SentinelDecision,
        intent: ExecutionIntent,
        execution_mode: str,
        now: datetime,
    ) -> ExecutionSafetyDecision:
        """Register and inspect an attempt. It never crosses to a broker in Phase 10."""
        registered = self.register_intent(intent=intent, execution_mode=execution_mode, now=now)
        record = registered.execution
        if registered.reason_codes:
            return registered
        if record.state is not ExecutionLifecycleState.READY_FOR_SUBMISSION:
            return ExecutionSafetyDecision(execution=record, reason_codes=("DUPLICATE_EXECUTION",))
        with self._state_store.submission_guard(record.execution_id) as acquired:
            if not acquired:
                return ExecutionSafetyDecision(execution=record, reason_codes=("SUBMISSION_ALREADY_IN_PROGRESS",))
            codes = self._gate_codes(proposal=proposal, decision=decision, intent=intent, execution_mode=execution_mode, now=now)
            if codes:
                state = ExecutionLifecycleState.KILL_SWITCHED if "KILL_SWITCH_ACTIVE" in codes else ExecutionLifecycleState.BLOCKED
                blocked = self._state_store.transition(execution_id=record.execution_id, to_state=state, now=now, reason_code=codes[0])
                return ExecutionSafetyDecision(execution=blocked, reason_codes=codes)
            # This invariant is deliberate: Phase 10 implements the control path,
            # not live authorization. No call to adapter.submit() is reachable.
            blocked = self._state_store.transition(execution_id=record.execution_id, to_state=ExecutionLifecycleState.BLOCKED, now=now, reason_code="EXECUTION_DISABLED")
            return ExecutionSafetyDecision(execution=blocked, reason_codes=("EXECUTION_DISABLED",))

    def recover_after_restart(self, *, intent: ExecutionIntent, execution_mode: str, now: datetime) -> ExecutionSafetyDecision:
        """Require reconciliation because process-local storage cannot prove broker absence."""
        record = build_execution_record(intent, execution_mode=execution_mode, now=now)
        recovery = self._state_store.mark_restart_recovery_required(record, now=now)
        return ExecutionSafetyDecision(execution=recovery, reason_codes=("STALE_EXECUTION_STATE", "RECONCILIATION_REQUIRED"))

    def _gate_codes(self, *, proposal: TradeProposal, decision: SentinelDecision, intent: ExecutionIntent, execution_mode: str, now: datetime) -> tuple[str, ...]:
        codes: list[str] = []
        if self._kill_switch.status().active:
            codes.append("KILL_SWITCH_ACTIVE")
        if execution_mode.strip().lower() != "live":
            codes.append("EXECUTION_DISABLED")
        if now >= intent.expires_at:
            codes.append("INTENT_EXPIRED")
        if decision.expires_at is None or now >= decision.expires_at:
            codes.append("APPROVAL_EXPIRED")
        if intent.proposal_id != proposal.proposal_id or intent.proposal_fingerprint != proposal_fingerprint(proposal):
            codes.append("PROPOSAL_MISMATCH")
        if intent.analysis_id != proposal.analysis_id or intent.evidence_fingerprint != proposal.evidence_fingerprint:
            codes.append("INTENT_LINEAGE_MISMATCH")
        if intent.approval_id != decision.approval_id or intent.policy_fingerprint != decision.policy_fingerprint or intent.assessment_fingerprint != decision.assessment_fingerprint:
            codes.append("APPROVAL_LINEAGE_MISMATCH")
        if not intent.account_id or intent.account_id != decision.account_id:
            codes.append("ACCOUNT_MISMATCH")
        try:
            self._approval_registry.verify(proposal=proposal, decision=decision, now=now)
        except ApprovalClaimError:
            codes.append("APPROVAL_INVALID")
        if self._adapter is None:
            codes.append("MT5_UNAVAILABLE")
        else:
            availability = self._adapter.availability()
            if availability != "MT5_CONNECTED":
                codes.append("MT5_UNAVAILABLE")
            else:
                prepared = self._adapter.prepare(intent)
                if prepared.status in {
                    ExecutionStatus.NOT_EXECUTED,
                    ExecutionStatus.ADAPTER_UNAVAILABLE,
                    ExecutionStatus.FAILED,
                }:
                    codes.extend(
                        code for code in prepared.reason_codes if code != "MT5_EXECUTION_DISABLED"
                    )
        return tuple(sorted(set(codes)))
