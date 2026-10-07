from datetime import UTC, datetime

from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import ExecutionLifecycleState, build_execution_record
from app.services.execution_gate import FinalExecutionGate
from app.services.trade_proposal_workflow import ApprovalRegistry
from tests.test_execution import _intent


def test_lost_process_local_state_requires_reconciliation() -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    intent = _intent(account_id="account-1")
    store = ExecutionStateStore()
    gate = FinalExecutionGate(approval_registry=ApprovalRegistry(), state_store=store, kill_switch=ExecutionKillSwitch(), adapter=None)
    recovery = gate.recover_after_restart(intent=intent, execution_mode="disabled", now=now)
    assert recovery.execution.state is ExecutionLifecycleState.RECONCILIATION_REQUIRED
    assert recovery.reason_codes == ("STALE_EXECUTION_STATE", "RECONCILIATION_REQUIRED")
