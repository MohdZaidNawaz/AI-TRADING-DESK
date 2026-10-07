from datetime import UTC, datetime

from app.execution.pilot import PilotExecutionCoordinator
from app.models.execution import ExecutionResult, ExecutionStatus
from tests.test_execution import approved_execution_pair


class RecordingAdapter:
    def __init__(self) -> None:
        self.intent = None

    def availability(self): return "TEST"
    def prepare(self, intent):
        self.intent = intent
        return ExecutionResult(intent_id=intent.execution_intent_id, proposal_id=intent.proposal_id, status=ExecutionStatus.NOT_EXECUTED, reason_codes=("TEST_ADAPTER",), evaluated_at=datetime.now(UTC), adapter_status="TEST")
    def submit(self, intent): raise AssertionError("not used")
    def query(self, intent_id): raise AssertionError("not used")
    def cancel(self, intent_id): raise AssertionError("not used")


def test_pilot_depends_only_on_execution_adapter_and_preserves_lineage() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    adapter = RecordingAdapter()
    intent, result = PilotExecutionCoordinator(registry, adapter=adapter).prepare_execution(proposal=proposal, decision=decision, now=now)
    assert intent is not None
    assert adapter.intent is None
    assert result.reason_codes == ("FINAL_SAFETY_GATE_REQUIRED",)
    assert intent.proposal_id == proposal.proposal_id
    assert intent.approval_id == decision.approval_id
    assert registry.verify(proposal=proposal, decision=decision, now=now).approval_id == decision.approval_id
