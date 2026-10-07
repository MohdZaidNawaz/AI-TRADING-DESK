"""Stage 2E coverage for the non-submitting end-to-end dry-run boundary."""

from pathlib import Path

from app.audit.service import AuditService
from app.audit.store import AuditStore, AuditStoreUnavailable
from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.mt5_adapter import MT5ExecutionAdapter
from app.execution.pilot import PilotExecutionCoordinator
from app.execution.state_store import ExecutionStateStore
from app.models.execution_state import ExecutionLifecycleState
from app.models.execution_state import build_execution_record
from app.models.mt5 import MT5ExecutionMode
from app.services.execution_gate import FinalExecutionGate
from app.services.trade_proposal_workflow import ApprovalRegistry
from tests.test_execution import approved_execution_pair
from tests.test_mt5_adapter import FakeMT5, _settings


def _approved_pair_for_mt5():
    proposal, decision, _registry, now = approved_execution_pair()
    adapter = MT5ExecutionAdapter(
        settings=_settings(), mt5_module=FakeMT5(),
        symbol_mappings={"XAUUSD": "XAUUSD.a"}, execution_mode=MT5ExecutionMode.DRY_RUN,
    )
    account, codes = adapter.account_information()
    assert not codes and account is not None
    registry = ApprovalRegistry()
    decision = registry._record_approved_decision(
        proposal,
        decision.model_copy(update={"approval_id": None, "account_id": account.account.account_id}),
    )
    return proposal, decision, registry, now, adapter


def _pilot(*, registry, adapter, tmp_path, kill_switch=None):
    store = ExecutionStateStore()
    gate = FinalExecutionGate(
        approval_registry=registry,
        state_store=store,
        kill_switch=kill_switch or ExecutionKillSwitch(),
        adapter=adapter,
        audit_service=AuditService(AuditStore(tmp_path / "audit.sqlite3")),
    )
    return PilotExecutionCoordinator(registry, adapter=adapter, safety_gate=gate), gate, store


def test_valid_dry_run_reaches_mt5_validation_without_submission(tmp_path) -> None:
    proposal, decision, registry, now, adapter = _approved_pair_for_mt5()
    pilot, gate, store = _pilot(registry=registry, adapter=adapter, tmp_path=tmp_path)

    intent, result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)

    assert intent is not None
    assert result.status.value == "dry_run"
    assert result.reason_codes == ("MT5_DRY_RUN",)
    assert dict(result.safe_request)["broker_symbol"] == "XAUUSD.a"
    assert adapter._mt5.order_send_calls == 0
    assert store.count == 1
    record = store.get(
        build_execution_record(intent, execution_mode=adapter.execution_mode.value, now=now).execution_id
    )
    assert record is not None
    assert record.state is ExecutionLifecycleState.DRY_RUN_VALIDATED
    assert registry.verify(proposal=proposal, decision=decision, now=now).approval_id == decision.approval_id
    assert all("password" not in value.lower() for _, value in result.safe_request)
    assert {event.reason_code for event in gate._audit_service.events()} >= {
        "intent_registration", "intent_ready", "dry_run_validation_started",
        "dry_run_provider_validation_completed",
    }


def test_repeated_dry_run_is_idempotent_and_never_submits(tmp_path) -> None:
    proposal, decision, registry, now, adapter = _approved_pair_for_mt5()
    pilot, _gate, store = _pilot(registry=registry, adapter=adapter, tmp_path=tmp_path)

    first, first_result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)
    second, second_result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)

    assert first is not None and second is not None
    assert first.execution_intent_id == second.execution_intent_id
    assert first_result.status.value == second_result.status.value == "dry_run"
    assert first_result.safe_request == second_result.safe_request
    assert store.count == 1
    assert adapter._mt5.order_send_calls == 0


def test_dry_run_requires_explicit_execution_fields(tmp_path) -> None:
    proposal, decision, registry, now, adapter = _approved_pair_for_mt5()
    pilot, _gate, _store = _pilot(registry=registry, adapter=adapter, tmp_path=tmp_path)
    for field, reason in (
        ("quantity", "MISSING_EXECUTION_QUANTITY"),
        ("order_type", "MISSING_EXECUTION_ORDER_TYPE"),
        ("filling_mode", "MISSING_EXECUTION_FILLING_MODE"),
    ):
        missing = proposal.model_copy(
            update={"parameters": proposal.parameters.model_copy(update={field: None})}
        )
        intent, result = pilot.prepare_execution(proposal=missing, decision=decision, now=now)
        assert intent is None
        assert reason in result.reason_codes
    assert adapter._mt5.order_send_calls == 0


def test_dry_run_kill_switch_blocks_before_any_submission(tmp_path) -> None:
    proposal, decision, registry, now, adapter = _approved_pair_for_mt5()
    kill_switch = ExecutionKillSwitch()
    kill_switch.trigger(reason="stage2e-test", changed_by="test")
    pilot, _gate, _store = _pilot(registry=registry, adapter=adapter, tmp_path=tmp_path, kill_switch=kill_switch)

    _intent, result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)

    assert "KILL_SWITCH_ACTIVE" in result.reason_codes
    assert adapter._mt5.order_send_calls == 0


def test_dry_run_expired_or_mismatched_approval_fails_before_adapter_submission(tmp_path) -> None:
    proposal, decision, registry, now, adapter = _approved_pair_for_mt5()
    pilot, _gate, _store = _pilot(registry=registry, adapter=adapter, tmp_path=tmp_path)

    _intent, expired = pilot.prepare_execution(
        proposal=proposal, decision=decision.model_copy(update={"expires_at": now}), now=now
    )
    mismatched = proposal.model_copy(update={"analysis_id": "different-analysis"})
    _intent, mismatch = pilot.prepare_execution(proposal=mismatched, decision=decision, now=now)

    assert "APPROVAL_EXPIRED" in expired.reason_codes
    assert "APPROVAL_ANALYSIS_MISMATCH" in mismatch.reason_codes
    assert adapter._mt5.order_send_calls == 0


class _FailAtProviderAudit:
    def __init__(self) -> None:
        self.calls = 0

    def record(self, _event) -> None:
        self.calls += 1
        if self.calls == 4:
            raise AuditStoreUnavailable("test-only")


def test_dry_run_provider_audit_failure_blocks_before_any_submission(tmp_path) -> None:
    proposal, decision, registry, now, adapter = _approved_pair_for_mt5()
    store = ExecutionStateStore()
    gate = FinalExecutionGate(
        approval_registry=registry, state_store=store, kill_switch=ExecutionKillSwitch(),
        adapter=adapter, audit_service=_FailAtProviderAudit(),
    )
    pilot = PilotExecutionCoordinator(registry, adapter=adapter, safety_gate=gate)

    intent, result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)

    assert intent is not None
    assert result.reason_codes == ("AUDIT_PERSISTENCE_UNAVAILABLE",)
    record = store.get(build_execution_record(intent, execution_mode="dry_run", now=now).execution_id)
    assert record is not None and record.state is ExecutionLifecycleState.BLOCKED
    assert adapter._mt5.order_send_calls == 0


def test_live_mode_remains_hard_blocked(tmp_path) -> None:
    proposal, decision, registry, now, _dry_adapter = _approved_pair_for_mt5()
    adapter = MT5ExecutionAdapter(
        settings=_settings(), mt5_module=FakeMT5(),
        symbol_mappings={"XAUUSD": "XAUUSD.a"}, execution_mode=MT5ExecutionMode.LIVE,
    )
    pilot, _gate, _store = _pilot(registry=registry, adapter=adapter, tmp_path=tmp_path)

    _intent, result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)

    assert result.status.value == "not_executed"
    assert result.reason_codes == ("EXECUTION_DISABLED",)
    assert adapter._mt5.order_send_calls == 0
