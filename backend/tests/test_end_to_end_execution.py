from datetime import timedelta

from app.audit.service import AuditService
from app.audit.store import AuditStore
from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.pilot import PilotExecutionCoordinator
from app.execution.reconciliation import reconcile
from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import BrokerOrderObservation, ExecutionLifecycleState
from app.services.execution_gate import FinalExecutionGate
from tests.test_execution_gate import ConnectedAdapter, _intent
from tests.test_execution import approved_execution_pair


def test_mocked_end_to_end_lineage_stays_blocked_before_any_broker_submission(tmp_path) -> None:
    proposal, decision, registry, now = approved_execution_pair()
    store = ExecutionStateStore()
    audit = AuditService(AuditStore(tmp_path / "audit.sqlite3"))
    adapter = ConnectedAdapter()
    gate = FinalExecutionGate(
        approval_registry=registry, state_store=store, kill_switch=ExecutionKillSwitch(),
        adapter=adapter, audit_service=audit,
    )
    prepared_intent, result = PilotExecutionCoordinator(
        registry, adapter=adapter, safety_gate=gate
    ).prepare_execution(proposal=proposal, decision=decision, now=now)
    assert prepared_intent is not None
    assert prepared_intent.proposal_id == proposal.proposal_id
    assert prepared_intent.approval_id == decision.approval_id
    assert result.reason_codes == ("EXECUTION_DISABLED",)
    assert registry.verify(proposal=proposal, decision=decision, now=now).approval_id == decision.approval_id
    events = audit.events()
    assert {event.proposal_id for event in events} == {proposal.proposal_id}
    assert {event.intent_id for event in events} == {prepared_intent.execution_intent_id}
    assert all(event.approval_id == decision.approval_id for event in events)


def test_mocked_reconciliation_marks_unknown_broker_outcome_without_retry(tmp_path) -> None:
    proposal, decision, registry, now = approved_execution_pair()
    intent = _intent(proposal, decision, now)
    store = ExecutionStateStore()
    gate = FinalExecutionGate(
        approval_registry=registry, state_store=store, kill_switch=ExecutionKillSwitch(),
        adapter=ConnectedAdapter(), audit_service=AuditService(AuditStore(tmp_path / "audit.sqlite3")),
    )
    registered = gate.register_intent(intent=intent, execution_mode="dry_run", now=now).execution
    in_progress = store.transition(execution_id=registered.execution_id, to_state=ExecutionLifecycleState.SUBMISSION_IN_PROGRESS, now=now)
    submitted = store.transition(execution_id=in_progress.execution_id, to_state=ExecutionLifecycleState.SUBMITTED, now=now + timedelta(seconds=1))
    report = reconcile(
        store=store, record=submitted,
        broker=BrokerOrderObservation(status="UNKNOWN", account_id=submitted.account_id),
        now=now + timedelta(seconds=2),
    )
    assert report.status.value == "unknown"
    assert "RECONCILIATION_REQUIRED" in report.reason_codes
    assert store.get(submitted.execution_id).state is ExecutionLifecycleState.RECONCILIATION_REQUIRED
