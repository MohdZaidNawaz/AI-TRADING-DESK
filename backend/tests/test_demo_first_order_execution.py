"""Phase 12B first-order boundary tests using a non-broker fake only."""

from __future__ import annotations

from datetime import timedelta
import inspect

import pytest

from app.models.demo_authorization import DemoAuthorizationStatus
from app.models.demo_submission import (
    DemoSubmissionStatus,
    FirstDemoTestPermit,
    TestOnlyAdapterOutcome,
    TestOnlyAdapterResponse,
)
from app.services.demo_first_order_execution import (
    FirstDemoExecutionCoordinator,
    TestOnlyBoundaryReservationStore,
)
from app.services.execution_gate import FinalExecutionGate
from app.audit.store import AuditStoreError
from app.services.trade_proposal_workflow import ApprovalClaimError
from tests.test_demo_execution_authorization import _inputs
from tests.test_demo_execution_readiness import ACCOUNT, NOW, _account, _context, _instrument, _policy


class _NeverExecutionAdapter:
    """Fails immediately if Phase 12B accidentally accesses a real adapter path."""

    def availability(self):  # pragma: no cover - must never run
        raise AssertionError("Phase 12B must not inspect an execution adapter")

    def prepare(self, _intent):  # pragma: no cover - must never run
        raise AssertionError("Phase 12B must not prepare an execution adapter")

    def submit(self, _intent):  # pragma: no cover - must never run
        raise AssertionError("Phase 12B must not submit an execution adapter")


class _TestBoundaryAdapter:
    test_only = True

    def __init__(self, outcome: TestOnlyAdapterOutcome = TestOnlyAdapterOutcome.ACCEPTED) -> None:
        self.outcome = outcome
        self.requests = []

    def reach_test_boundary(self, request):
        self.requests.append(request)
        return TestOnlyAdapterResponse(outcome=self.outcome, observed_at=NOW)


def _setup(tmp_path, *, outcome: TestOnlyAdapterOutcome = TestOnlyAdapterOutcome.ACCEPTED, policy_max: float = 0.01):
    service, store, registry, audit, state_store, values = _inputs(tmp_path)
    policy = _policy(
        effective_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        max_order_volume=policy_max,
        max_total_open_volume=0.01,
    )
    intent = values["intent"].model_copy(update={"quantity": 0.01})
    values.update({"policy": policy, "intent": intent})
    authorized = service.authorize_demo_execution(**values).authorization
    assert authorized is not None
    permit = FirstDemoTestPermit(
        authorization_id=authorized.authorization_id,
        authorization_fingerprint=authorized.fingerprint,
        execution_intent_id=intent.execution_intent_id,
        execution_intent_fingerprint=intent.fingerprint,
        proposal_id=values["proposal"].proposal_id,
        proposal_fingerprint=intent.proposal_fingerprint,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=20),
    )
    fake = _TestBoundaryAdapter(outcome)
    final_gate = FinalExecutionGate(
        approval_registry=registry,
        state_store=state_store,
        kill_switch=service._kill_switch,
        adapter=_NeverExecutionAdapter(),
        audit_service=audit,
    )
    coordinator = FirstDemoExecutionCoordinator(
        authorization_service=service,
        authorization_store=store,
        approval_registry=registry,
        final_execution_gate=final_gate,
        reservation_store=TestOnlyBoundaryReservationStore(),
        audit_service=audit,
        test_adapter=fake,
    )
    return coordinator, fake, service, store, registry, audit, state_store, values, permit, authorized


def _fresh(tmp_path, name: str, **kwargs):
    path = tmp_path / name
    path.mkdir()
    return _setup(path, **kwargs)


def _attempt(coordinator, permit, values, **changes):
    return coordinator.attempt_test_only(
        permit=changes.pop("permit", permit), policy=changes.pop("policy", values["policy"]),
        proposal=changes.pop("proposal", values["proposal"]), decision=changes.pop("decision", values["decision"]),
        intent=changes.pop("intent", values["intent"]), context=changes.pop("context", values["context"]),
        runtime_execution_mode=changes.pop("runtime_execution_mode", "disabled"), now=changes.pop("now", NOW),
    )


def test_valid_synthetic_path_reaches_only_test_boundary_and_reserves_once(tmp_path) -> None:
    coordinator, fake, _service, store, registry, audit, state_store, values, permit, _auth = _setup(tmp_path)
    def production_registration_forbidden(**_kwargs):
        pytest.fail("Phase 12B must never call FinalExecutionGate.register_intent")

    coordinator._final_execution_gate.register_intent = production_registration_forbidden
    result = _attempt(coordinator, permit, values)

    assert result.status is DemoSubmissionStatus.RECONCILIATION_REQUIRED
    assert result.test_boundary_reached is True
    assert result.broker_submission_attempted is False and result.submission_allowed is False
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.quantity == 0.01
    assert request.order_type == "MARKET" and request.filling_mode == "FOK"
    assert request.stop_loss == values["intent"].stop_loss and request.take_profit == values["intent"].take_profit
    assert result.execution_id is None
    assert result.test_reservation_id is not None
    assert coordinator._reservation_store.get(result.test_reservation_id).status.value == "reconciliation_required"
    assert state_store.count == 0
    assert store.get(permit.authorization_id).status is DemoAuthorizationStatus.RECONCILIATION_REQUIRED
    with pytest.raises(ApprovalClaimError):
        registry.verify(proposal=values["proposal"], decision=values["decision"], now=NOW)
    assert any(event.state == "test_boundary_reserved" for event in audit.events())
    assert all("submitted" not in (event.state or "") and "filled" not in (event.state or "") for event in audit.events())


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"runtime_execution_mode": "dry_run"}, "RUNTIME_EXECUTION_MODE_NOT_DISABLED"),
        ({"decision": None}, "FIRST_DEMO_INPUTS_REQUIRED"),
        ({"intent": None}, "FIRST_DEMO_INPUTS_REQUIRED"),
        ({"context": None}, "FIRST_DEMO_INPUTS_REQUIRED"),
    ],
)
def test_global_and_required_gates_block_before_test_boundary(tmp_path, change, code) -> None:
    coordinator, fake, _service, _store, _registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "lineage")
    result = _attempt(coordinator, permit, values, **change)
    assert result.status is DemoSubmissionStatus.REJECTED
    assert code in result.reason_codes and fake.requests == [] and state_store.count == 0


@pytest.mark.parametrize(
    ("intent_change", "context_change", "code"),
    [
        ({"symbol": "XAU/USD"}, None, "DEMO_SYMBOL_MISMATCH"),
        ({"quantity": 0.02}, None, "DEMO_VOLUME_LIMIT"),
        ({"order_type": "LIMIT"}, None, "DEMO_UNSUPPORTED_ORDER_TYPE"),
        ({"filling_mode": "BOOK_OR_CANCEL"}, None, "DEMO_UNSUPPORTED_FILLING_MODE"),
        ({"stop_loss": None}, None, "DEMO_INVALID_PRICE"),
        ({"take_profit": None}, None, "DEMO_INVALID_PRICE"),
        ({"stop_loss": 120.0}, None, "DEMO_INVALID_STOP_DIRECTION"),
        ({}, {"market_observed_at": NOW - timedelta(minutes=2)}, "DEMO_MARKET_DATA_STALE"),
        ({}, {"instrument": _instrument(minimum_volume=0.1)}, "FIRST_DEMO_BROKER_VOLUME_INVALID"),
    ],
)
def test_order_shape_market_and_broker_constraints_fail_closed(tmp_path, intent_change, context_change, code) -> None:
    coordinator, fake, _service, _store, _registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "account")
    intent = values["intent"].model_copy(update=intent_change)
    context = _context(**context_change) if context_change else values["context"]
    result = _attempt(coordinator, permit, values, intent=intent, context=context)
    assert result.status is DemoSubmissionStatus.REJECTED
    assert code in result.reason_codes and fake.requests == [] and state_store.count == 0


def test_policy_cap_account_lineage_and_permit_lineage_block(tmp_path) -> None:
    coordinator, fake, _service, _store, _registry, _audit, state_store, values, permit, _auth = _setup(tmp_path, policy_max=1.0)
    cap = _attempt(coordinator, permit, values)
    assert "FIRST_DEMO_VOLUME_MUST_BE_EXACTLY_0_01" in cap.reason_codes

    coordinator, fake, _service, _store, _registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "permit")
    from app.models.portfolio_risk import SourceAccountKey
    other = SourceAccountKey(source_id="mt5", source_account_id="other", account_reference=ACCOUNT.account_reference)
    wrong_account = _attempt(coordinator, permit, values, context=_context(account=_account(other), account_id=other.account_id))
    assert wrong_account.status is DemoSubmissionStatus.REJECTED

    coordinator, fake, _service, _store, _registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "lineage")
    changed = permit.model_copy(update={"proposal_id": "other"})
    mismatch = _attempt(coordinator, changed, values)
    assert "TEST_ONLY_PERMIT_LINEAGE_MISMATCH" in mismatch.reason_codes
    assert fake.requests == [] and state_store.count == 0


def test_authorization_status_kill_switch_audit_and_adapter_fail_closed(tmp_path) -> None:
    coordinator, fake, service, store, _registry, _audit, state_store, values, permit, _auth = _setup(tmp_path)
    store.consume_for_future_submission(authorization_id=permit.authorization_id, now=NOW)
    consumed = _attempt(coordinator, permit, values)
    assert "DEMO_AUTHORIZATION_CONSUMED" in consumed.reason_codes and fake.requests == []

    coordinator, fake, service, _store, _registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "killed")
    service._kill_switch.trigger(reason="synthetic", changed_by="test")
    killed = _attempt(coordinator, permit, values)
    assert "KILL_SWITCH_ACTIVE" in killed.reason_codes and fake.requests == []

    coordinator, fake, service, _store, registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "audit")
    final_gate = FinalExecutionGate(approval_registry=registry, state_store=state_store, kill_switch=service._kill_switch, adapter=_NeverExecutionAdapter(), audit_service=None)
    no_audit = FirstDemoExecutionCoordinator(authorization_service=service, authorization_store=coordinator._authorization_store, approval_registry=registry, final_execution_gate=final_gate, reservation_store=TestOnlyBoundaryReservationStore(), audit_service=None, test_adapter=fake)
    unavailable = _attempt(no_audit, permit, values)
    assert "AUDIT_PERSISTENCE_UNAVAILABLE" in unavailable.reason_codes and fake.requests == []

    coordinator, _fake, _service, _store, _registry, _audit, state_store, values, permit, _auth = _fresh(tmp_path, "adapter")
    coordinator._test_adapter = object()
    missing = _attempt(coordinator, permit, values)
    assert "TEST_ONLY_ADAPTER_REQUIRED" in missing.reason_codes and state_store.count == 0


def test_audit_write_failure_blocks_before_test_only_boundary_and_preserves_authority(tmp_path) -> None:
    coordinator, fake, _service, store, registry, _audit, state_store, values, permit, _auth = _setup(tmp_path)

    class _FailingAudit:
        def record(self, _event):
            raise AuditStoreError("synthetic audit failure")

    coordinator._audit_service = _FailingAudit()
    result = _attempt(coordinator, permit, values)

    assert result.status is DemoSubmissionStatus.REJECTED
    assert "AUDIT_PERSISTENCE_UNAVAILABLE" in result.reason_codes
    assert fake.requests == [] and state_store.count == 0
    assert store.get(permit.authorization_id).status is DemoAuthorizationStatus.AUTHORIZED
    assert registry.verify(proposal=values["proposal"], decision=values["decision"], now=NOW)


@pytest.mark.parametrize("outcome", (TestOnlyAdapterOutcome.REJECTED, TestOnlyAdapterOutcome.TIMEOUT, TestOnlyAdapterOutcome.CONNECTION_FAILURE, TestOnlyAdapterOutcome.UNKNOWN))
def test_failed_or_uncertain_test_outcomes_never_retry(outcome, tmp_path) -> None:
    coordinator, fake, _service, store, _registry, _audit, state_store, values, permit, _auth = _setup(tmp_path, outcome=outcome)
    first = _attempt(coordinator, permit, values)
    assert len(fake.requests) == 1
    if outcome is TestOnlyAdapterOutcome.REJECTED:
        assert first.status is DemoSubmissionStatus.REJECTED
        assert coordinator._reservation_store.get(first.test_reservation_id).status.value == "rejected"
        assert store.get(permit.authorization_id).status is DemoAuthorizationStatus.CONSUMED
    else:
        assert first.status is DemoSubmissionStatus.RECONCILIATION_REQUIRED
        assert coordinator._reservation_store.get(first.test_reservation_id).status.value == "reconciliation_required"
    second = _attempt(coordinator, permit, values)
    assert second.status is DemoSubmissionStatus.REJECTED
    assert len(fake.requests) == 1


def test_restart_recovery_and_static_safety(tmp_path) -> None:
    coordinator, fake, _service, store, _registry, _audit, state_store, values, permit, _auth = _setup(tmp_path)
    recovery = coordinator.recover_after_restart(permit=permit, intent=values["intent"], now=NOW)
    assert recovery.status is DemoSubmissionStatus.RECONCILIATION_REQUIRED
    assert store.get(permit.authorization_id).status is DemoAuthorizationStatus.RECONCILIATION_REQUIRED
    assert fake.requests == [] and state_store.count == 0
    after_restart = _attempt(coordinator, permit, values)
    assert after_restart.status is DemoSubmissionStatus.REJECTED
    assert fake.requests == []
    source = inspect.getsource(FirstDemoExecutionCoordinator)
    assert "order_send" not in source and ".submit(" not in source and "MT5ExecutionAdapter" not in source
    assert "register_intent" not in source and "ExecutionStateStore" not in source and ".transition(" not in source
