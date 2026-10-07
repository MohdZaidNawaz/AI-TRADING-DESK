from datetime import timedelta

from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.pilot import PilotExecutionCoordinator
from app.execution.state_store import ExecutionStateStore
from app.models.execution import ExecutionIntent, ExecutionResult, ExecutionStatus
from app.services.execution_gate import FinalExecutionGate
from tests.test_execution import approved_execution_pair


class ConnectedAdapter:
    def availability(self): return "MT5_CONNECTED"
    def prepare(self, intent):
        return ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=intent.proposal_id, status=ExecutionStatus.VALIDATED, evaluated_at=intent.created_at, adapter_status="test")
    def submit(self, _intent): raise AssertionError("final gate must not submit")
    def query(self, _intent_id): raise AssertionError("not used")
    def cancel(self, _intent_id): raise AssertionError("not used")


class ClosedMarketAdapter(ConnectedAdapter):
    def prepare(self, intent):
        return ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=intent.proposal_id, status=ExecutionStatus.NOT_EXECUTED, reason_codes=("MT5_MARKET_CLOSED",), evaluated_at=intent.created_at, adapter_status="test")


def _intent(proposal, decision, now, **changes):
    values = dict(
        proposal_id=proposal.proposal_id, proposal_fingerprint=decision.proposal_fingerprint,
        analysis_id=proposal.analysis_id, symbol=proposal.symbol, asset_class=proposal.asset_class,
        direction=proposal.parameters.direction, requested_entry=proposal.parameters.reference_price,
        stop_loss=proposal.parameters.stop_loss, take_profit=proposal.parameters.targets[0],
        requested_risk_fraction=proposal.parameters.requested_risk_fraction, quantity=1.0,
        order_type="MARKET", filling_mode="FOK", approval_id=decision.approval_id,
        policy_id=decision.policy_id, policy_fingerprint=decision.policy_fingerprint,
        assessment_id=decision.assessment_id, assessment_fingerprint=decision.assessment_fingerprint,
        evidence_fingerprint=proposal.evidence_fingerprint, account_id=decision.account_id,
        portfolio_snapshot_id=decision.portfolio_snapshot_id,
        portfolio_snapshot_fingerprint=decision.portfolio_snapshot_fingerprint,
        created_at=now, expires_at=decision.expires_at,
    )
    values.update(changes)
    return ExecutionIntent(**values)


def test_final_gate_requires_exact_approval_and_never_consumes_it() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    gate = FinalExecutionGate(approval_registry=registry, state_store=ExecutionStateStore(), kill_switch=ExecutionKillSwitch(), adapter=ConnectedAdapter())
    result = gate.evaluate_submission(proposal=proposal, decision=decision, intent=_intent(proposal, decision, now), execution_mode="live", now=now)
    assert not result.submission_allowed
    assert result.reason_codes == ("EXECUTION_DISABLED",)
    assert registry.verify(proposal=proposal, decision=decision, now=now).approval_id == decision.approval_id


def test_kill_switch_and_expiry_block_before_any_submission() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    switch = ExecutionKillSwitch(); switch.trigger(reason="test", changed_by="tester")
    gate = FinalExecutionGate(approval_registry=registry, state_store=ExecutionStateStore(), kill_switch=switch, adapter=ConnectedAdapter())
    result = gate.evaluate_submission(proposal=proposal, decision=decision, intent=_intent(proposal, decision, now), execution_mode="live", now=now)
    assert "KILL_SWITCH_ACTIVE" in result.reason_codes
    assert result.execution.state.value == "kill_switched"
    expired = gate.recover_after_restart(intent=_intent(proposal, decision, now, expires_at=now), execution_mode="live", now=now)
    assert "RECONCILIATION_REQUIRED" in expired.reason_codes


def test_missing_adapter_and_lineage_mismatch_fail_closed() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    gate = FinalExecutionGate(approval_registry=registry, state_store=ExecutionStateStore(), kill_switch=ExecutionKillSwitch(), adapter=None)
    result = gate.evaluate_submission(proposal=proposal, decision=decision, intent=_intent(proposal, decision, now, analysis_id="other"), execution_mode="disabled", now=now)
    assert "MT5_UNAVAILABLE" in result.reason_codes
    assert "INTENT_LINEAGE_MISMATCH" in result.reason_codes


def test_final_gate_propagates_safe_adapter_validation_blocks() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    gate = FinalExecutionGate(approval_registry=registry, state_store=ExecutionStateStore(), kill_switch=ExecutionKillSwitch(), adapter=ClosedMarketAdapter())
    result = gate.evaluate_submission(proposal=proposal, decision=decision, intent=_intent(proposal, decision, now), execution_mode="live", now=now)
    assert "MT5_MARKET_CLOSED" in result.reason_codes
    assert result.execution.state.value == "blocked"


def test_pilot_routes_adapter_preparation_through_final_safety_gate() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    gate = FinalExecutionGate(approval_registry=registry, state_store=ExecutionStateStore(), kill_switch=ExecutionKillSwitch(), adapter=ConnectedAdapter())
    intent, result = PilotExecutionCoordinator(registry, adapter=ConnectedAdapter(), safety_gate=gate).prepare_execution(proposal=proposal, decision=decision, now=now)
    assert intent is not None
    assert result.reason_codes == ("EXECUTION_DISABLED",)
    assert registry.verify(proposal=proposal, decision=decision, now=now).approval_id == decision.approval_id
