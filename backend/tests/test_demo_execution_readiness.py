"""Stage 3 controlled-demo readiness tests. All MT5 facts are synthetic."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.audit.service import AuditService
from app.audit.store import AuditStore
from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.reconciliation import reconcile
from app.execution.state_store import ExecutionStateStore
from app.models.demo_execution import (
    ControlledDemoExecutionPolicy,
    DemoInstrumentBinding,
    DemoReadinessContext,
)
from app.models.execution import ExecutionIntent
from app.models.execution_state import BrokerOrderObservation, ExecutionLifecycleState, build_execution_record
from app.models.events import AssetClass
from app.models.mt5 import BrokerInstrumentContract, MT5AccountSnapshot, MT5PositionSnapshot
from app.models.portfolio_risk import SourceAccountKey
from app.services.demo_execution_readiness import ControlledDemoReadinessGate
from app.services.trade_proposal_workflow import ApprovalRegistry, proposal_fingerprint
from tests.test_execution import approved_execution_pair


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
ACCOUNT = SourceAccountKey(source_id="mt5", source_account_id="demo-123", account_reference="Demo-Server")
OTHER_ACCOUNT = SourceAccountKey(source_id="mt5", source_account_id="other", account_reference="Demo-Server")


def _policy(**changes: object) -> ControlledDemoExecutionPolicy:
    values: dict[str, object] = {
        "policy_id": "demo-readiness", "policy_version": "v1", "allowed_account": ACCOUNT,
        "instrument": DemoInstrumentBinding(canonical_symbol="XAUUSD", broker_symbol="XAUUSD"),
        "risk_currency": "USD", "allowed_order_types": ("MARKET",), "allowed_filling_modes": ("FOK", "IOC"),
        "max_order_volume": 1.0, "max_total_open_volume": 4.0, "max_open_positions": 3,
        "max_executions_in_window": 1, "controlled_window_seconds": 300,
        "max_monetary_risk_per_execution": 10_000.0, "max_aggregate_monetary_risk": 20_000.0,
        "max_stop_distance": 20.0, "max_order_notional": 500_000.0, "max_market_age_seconds": 30,
    }
    values.update(changes)
    return ControlledDemoExecutionPolicy(**values)


def _instrument(**changes: object) -> BrokerInstrumentContract:
    values: dict[str, object] = {
        "canonical_symbol": "XAUUSD", "broker_symbol": "XAUUSD", "digits": 2, "point": 0.01,
        "tick_size": 0.01, "tick_value": 1.0, "contract_size": 100.0,
        "minimum_volume": 0.01, "maximum_volume": 10.0, "volume_step": 0.01,
        "filling_modes": ("FOK", "IOC"), "order_modes": ("MARKET",),
        "visible": True, "tradable": True, "quote_available": True,
    }
    values.update(changes)
    return BrokerInstrumentContract(**values)


def _account(key: SourceAccountKey = ACCOUNT) -> MT5AccountSnapshot:
    return MT5AccountSnapshot(account=key, balance=10_000.0, equity=10_000.0, margin=0.0, free_margin=10_000.0, currency="USD", trading_allowed=True, terminal_trade_allowed=True)


def _position(ticket: int = 1, **changes: object) -> MT5PositionSnapshot:
    values: dict[str, object] = {"ticket": ticket, "symbol": "XAUUSD", "volume": 0.1, "price_open": 100.0, "stop_loss": 95.0}
    values.update(changes)
    return MT5PositionSnapshot(**values)


def _pair():
    proposal, decision, _old_registry, _old_now = approved_execution_pair()
    registry = ApprovalRegistry()
    decision = registry._record_approved_decision(proposal, decision.model_copy(update={"approval_id": None, "account_id": ACCOUNT.account_id, "expires_at": NOW + timedelta(minutes=5)}))
    params = proposal.parameters
    assert params is not None and params.direction is not None and params.reference_price is not None and params.stop_loss is not None and params.targets
    intent = ExecutionIntent(
        proposal_id=proposal.proposal_id, proposal_fingerprint=proposal_fingerprint(proposal), analysis_id=proposal.analysis_id,
        symbol="XAUUSD", asset_class=AssetClass.COMMODITY, direction=params.direction,
        requested_entry=params.reference_price, stop_loss=params.stop_loss, take_profit=params.targets[0],
        requested_risk_fraction=params.requested_risk_fraction, quantity=1.0, order_type="MARKET", filling_mode="FOK",
        approval_id=decision.approval_id or "", policy_id=decision.policy_id, policy_fingerprint=decision.policy_fingerprint,
        assessment_id=decision.assessment_id, assessment_fingerprint=decision.assessment_fingerprint,
        evidence_fingerprint=proposal.evidence_fingerprint, account_id=ACCOUNT.account_id,
        portfolio_snapshot_id=decision.portfolio_snapshot_id, portfolio_snapshot_fingerprint=decision.portfolio_snapshot_fingerprint,
        created_at=NOW, expires_at=decision.expires_at or NOW,
    )
    return proposal, decision, registry, intent


def _context(**changes: object) -> DemoReadinessContext:
    values: dict[str, object] = {"account": _account(), "account_id": ACCOUNT.account_id, "instrument": _instrument(), "positions": (), "execution_history": (), "market_observed_at": NOW, "captured_at": NOW}
    values.update(changes)
    return DemoReadinessContext(**values)


def _assessment(**changes: object):
    proposal, decision, registry, intent = _pair()
    values = {"policy": _policy(), "proposal": proposal, "decision": decision, "intent": intent, "context": _context(), "now": NOW}
    values.update(changes)
    return ControlledDemoReadinessGate(approval_registry=registry, kill_switch=ExecutionKillSwitch()).evaluate(**values)


def test_policy_is_immutable_versioned_disabled_and_xauusd_only() -> None:
    policy = _policy()
    assert policy.enabled is False and policy.mode.value == "demo_controlled" and len(policy.fingerprint) == 64
    with pytest.raises(ValidationError):
        DemoInstrumentBinding(canonical_symbol="XAUUSD", broker_symbol="XAUUSD.a")
    with pytest.raises(ValidationError):
        policy.enabled = True
    with pytest.raises(ValidationError):
        _policy(enabled=True)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"context": _context(account=_account(OTHER_ACCOUNT), account_id=OTHER_ACCOUNT.account_id)}, "DEMO_ACCOUNT_MISMATCH"),
        ({"context": _context(instrument=_instrument(broker_symbol="OTHER"))}, "DEMO_SYMBOL_MISMATCH"),
        ({"intent": _pair()[3].model_copy(update={"symbol": "XAGUSD"})}, "DEMO_SYMBOL_MISMATCH"),
        ({"intent": _pair()[3].model_copy(update={"quantity": 2.0})}, "DEMO_VOLUME_LIMIT"),
        ({"context": _context(positions=(_position(1), _position(2), _position(3)))}, "DEMO_POSITION_COUNT_LIMIT"),
        ({"policy": _policy(max_total_open_volume=1.1), "context": _context(positions=(_position(1), _position(2)))}, "DEMO_TOTAL_OPEN_VOLUME_LIMIT"),
        ({"policy": _policy(max_monetary_risk_per_execution=1.0)}, "DEMO_MONETARY_RISK_LIMIT"),
        ({"intent": _pair()[3].model_copy(update={"stop_loss": None})}, "DEMO_INVALID_PRICE"),
        ({"intent": _pair()[3].model_copy(update={"expires_at": NOW})}, "INTENT_EXPIRED"),
        ({"intent": _pair()[3].model_copy(update={"order_type": "LIMIT"})}, "DEMO_UNSUPPORTED_ORDER_TYPE"),
        ({"intent": _pair()[3].model_copy(update={"filling_mode": "BOOK_OR_CANCEL"})}, "DEMO_UNSUPPORTED_FILLING_MODE"),
        ({"context": _context(market_observed_at=NOW - timedelta(minutes=1))}, "DEMO_MARKET_DATA_STALE"),
    ],
)
def test_demo_readiness_fails_closed_for_limits_and_bindings(change: dict[str, object], code: str) -> None:
    assessment = _assessment(**change)
    assert code in assessment.reason_codes
    assert assessment.submission_allowed is False


def test_kill_switch_expired_approval_duplicate_and_window_are_blocked() -> None:
    proposal, decision, registry, intent = _pair()
    history = (build_execution_record(intent, execution_mode="demo_controlled", now=NOW),)
    policy = _policy(max_executions_in_window=1)
    gate = ControlledDemoReadinessGate(approval_registry=registry, kill_switch=ExecutionKillSwitch())
    duplicate = gate.evaluate(policy=policy, proposal=proposal, decision=decision, intent=intent, context=_context(execution_history=history), now=NOW)
    assert {"DEMO_DUPLICATE_EXECUTION", "DEMO_EXECUTION_WINDOW_LIMIT"}.issubset(duplicate.reason_codes)
    switch = ExecutionKillSwitch(); switch.trigger(reason="test", changed_by="tester")
    blocked = ControlledDemoReadinessGate(approval_registry=registry, kill_switch=switch).evaluate(policy=policy, proposal=proposal, decision=decision, intent=intent, context=_context(), now=NOW)
    assert "KILL_SWITCH_ACTIVE" in blocked.reason_codes
    expired = gate.evaluate(policy=policy, proposal=proposal, decision=decision.model_copy(update={"expires_at": NOW}), intent=intent, context=_context(), now=NOW)
    assert "DEMO_SENTINEL_APPROVAL_INVALID" in expired.reason_codes


def test_existing_position_without_exact_risk_inputs_fails_closed() -> None:
    assessment = _assessment(context=_context(positions=(_position(symbol="OTHER"),)))
    assert "DEMO_EXISTING_POSITION_RISK_UNAVAILABLE" in assessment.reason_codes


def test_policy_configuration_and_context_are_deterministic_and_immutable() -> None:
    first = _context(positions=(_position(2), _position(1)))
    second = _context(positions=(_position(1), _position(2)))
    assert first.positions == second.positions
    with pytest.raises(ValidationError):
        _policy(allowed_filling_modes=("BOOK_OR_CANCEL",))
    with pytest.raises(ValidationError):
        _context(account_id="different-account")
    assert _assessment().reason_codes == ("DEMO_CONTROLLED_DISABLED",)


def test_readiness_audit_is_sanitized_and_contains_no_submission_event(tmp_path) -> None:
    proposal, decision, registry, intent = _pair()
    audit = AuditService(AuditStore(tmp_path / "audit.sqlite3"))
    assessment = ControlledDemoReadinessGate(
        approval_registry=registry, kill_switch=ExecutionKillSwitch(), audit_service=audit
    ).evaluate(policy=_policy(), proposal=proposal, decision=decision, intent=intent, context=_context(), now=NOW)
    event = audit.events()[0]
    assert assessment.submission_allowed is False
    assert event.state == "demo_readiness_blocked"
    assert all(forbidden not in str(event).lower() for forbidden in ("password", "api_key", "secret", "token"))


def test_synthetic_unknown_and_rejected_broker_observations_require_safe_terminal_states() -> None:
    _proposal, _decision, _registry, intent = _pair()

    def submitted() -> tuple[ExecutionStateStore, object]:
        store = ExecutionStateStore()
        record, _ = store.create_or_get(build_execution_record(intent, execution_mode="demo_controlled", now=NOW))
        for state in (ExecutionLifecycleState.VALIDATED, ExecutionLifecycleState.READY_FOR_SUBMISSION, ExecutionLifecycleState.SUBMISSION_IN_PROGRESS, ExecutionLifecycleState.SUBMITTED):
            record = store.transition(execution_id=record.execution_id, to_state=state, now=NOW)
        return store, record

    store, record = submitted()
    unknown = reconcile(store=store, record=record, broker=BrokerOrderObservation(status="UNKNOWN", account_id=ACCOUNT.account_id), now=NOW)
    assert unknown.status.value == "unknown"
    assert store.get(record.execution_id).state is ExecutionLifecycleState.RECONCILIATION_REQUIRED
    store, record = submitted()
    rejected = reconcile(store=store, record=record, broker=BrokerOrderObservation(status="REJECTED", account_id=ACCOUNT.account_id), now=NOW)
    assert rejected.status.value == "rejected"
    assert store.get(record.execution_id).state is ExecutionLifecycleState.REJECTED
