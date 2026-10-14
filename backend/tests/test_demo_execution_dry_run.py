"""Phase 12A tests for the strictly non-submitting controlled demo path.

Every dependency is synthetic.  The exploding adapter proves that this
workflow neither prepares nor submits a broker request.
"""

from __future__ import annotations

from datetime import timedelta
import inspect

import pytest
from pydantic import ValidationError

from app.execution.state_store import ExecutionStateStore
from app.models.demo_authorization import DemoAuthorizationStatus
from app.models.demo_dry_run import ControlledDemoDryRunResult, DemoDryRunStatus
from app.models.execution_state import ExecutionLifecycleState, build_execution_record
from app.services.demo_execution_dry_run import ControlledDemoDryRunWorkflow
from app.services.execution_gate import FinalExecutionGate
from app.services.trade_proposal_workflow import proposal_fingerprint
from tests.test_demo_execution_authorization import _authorize, _inputs
from tests.test_demo_execution_readiness import NOW, _context, _instrument, _policy


class _ExplodingAdapter:
    """A test double which makes any accidental execution-boundary call fail."""

    calls = 0

    def availability(self):  # pragma: no cover - must never be reached
        self.calls += 1
        raise AssertionError("dry-run workflow must not inspect an adapter")

    def prepare(self, _intent):  # pragma: no cover - must never be reached
        self.calls += 1
        raise AssertionError("dry-run workflow must not prepare an adapter")

    def submit(self, _intent):  # pragma: no cover - must never be reached
        self.calls += 1
        raise AssertionError("dry-run workflow must not submit an adapter")


def _workflow(service, registry, state_store, audit):
    adapter = _ExplodingAdapter()
    gate = FinalExecutionGate(
        approval_registry=registry,
        state_store=state_store,
        kill_switch=service._kill_switch,
        adapter=adapter,
        audit_service=audit,
    )
    return ControlledDemoDryRunWorkflow(
        authorization_service=service,
        final_execution_gate=gate,
        audit_service=audit,
    ), adapter


def _validated(tmp_path):
    service, store, registry, audit, state_store, values, preauth = _authorize(tmp_path)
    authorization = preauth.authorization
    assert authorization is not None
    workflow, adapter = _workflow(service, registry, state_store, audit)
    result = workflow.validate(
        authorization_id=authorization.authorization_id,
        policy=values["policy"], proposal=values["proposal"], decision=values["decision"],
        intent=values["intent"], context=values["context"], runtime_execution_mode="disabled", now=NOW,
    )
    return service, store, registry, audit, state_store, values, authorization, workflow, adapter, result


def _fresh(tmp_path, name: str):
    path = tmp_path / name
    path.mkdir()
    return _validated(path)


def _validate(workflow, authorization, values, **changes):
    return workflow.validate(
        authorization_id=changes.pop("authorization_id", authorization.authorization_id),
        policy=changes.pop("policy", values["policy"]),
        proposal=changes.pop("proposal", values["proposal"]),
        decision=changes.pop("decision", values["decision"]),
        intent=changes.pop("intent", values["intent"]),
        context=changes.pop("context", values["context"]),
        runtime_execution_mode=changes.pop("runtime_execution_mode", "disabled"),
        now=changes.pop("now", NOW),
    )


def test_valid_synthetic_dry_run_is_non_submitting_and_preserves_authority(tmp_path) -> None:
    service, store, registry, audit, state_store, values, authorization, _workflow_, adapter, result = _validated(tmp_path)

    assert result.status is DemoDryRunStatus.VALIDATED
    assert result.reason_codes == ()
    assert result.submission_attempted is False
    assert result.submission_allowed is False
    assert result.execution_state_mutated is False
    assert adapter.calls == 0 and state_store.count == 0
    registry.verify(proposal=values["proposal"], decision=values["decision"], now=NOW)
    assert store.get(authorization.authorization_id).status is DemoAuthorizationStatus.AUTHORIZED
    assert {event.state for event in audit.events() if event.event_type.value == "demo_dry_run"} == {
        "dry_run_requested", "dry_run_validated"
    }


def test_identical_validation_is_deterministic_and_idempotent(tmp_path) -> None:
    _service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, first = _validated(tmp_path)
    second = _validate(workflow, authorization, values)

    assert first.status is second.status is DemoDryRunStatus.VALIDATED
    assert first.dry_run_id == second.dry_run_id
    assert first.fingerprint == second.fingerprint
    assert state_store.count == 0 and adapter.calls == 0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"authorization_id": "missing-authorization"}, "DEMO_AUTHORIZATION_NOT_FOUND"),
        ({"runtime_execution_mode": "dry_run"}, "RUNTIME_EXECUTION_MODE_NOT_DISABLED"),
        ({"proposal": None}, "DEMO_AUTHORIZATION_VERIFICATION_INPUT_MISSING"),
        ({"decision": None}, "DEMO_SENTINEL_APPROVAL_REQUIRED"),
        ({"intent": None}, "DEMO_EXECUTION_INTENT_REQUIRED"),
        ({"context": None}, "DEMO_AUTHORIZATION_VERIFICATION_INPUT_MISSING"),
    ],
)
def test_required_boundary_inputs_fail_closed(tmp_path, change, code) -> None:
    _service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _validated(tmp_path)
    outcome = _validate(workflow, authorization, values, **change)
    assert outcome.status is DemoDryRunStatus.REJECTED
    assert outcome.submission_attempted is False and outcome.submission_allowed is False
    assert code in outcome.reason_codes
    assert state_store.count == 0 and adapter.calls == 0


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("direction", None, "DEMO_EXECUTION_DIRECTION_REQUIRED"),
        ("quantity", None, "DEMO_EXECUTION_PARAMETERS_REQUIRED"),
        ("order_type", None, "DEMO_EXECUTION_PARAMETERS_REQUIRED"),
        ("filling_mode", None, "DEMO_EXECUTION_PARAMETERS_REQUIRED"),
        ("requested_entry", None, "DEMO_EXECUTION_PARAMETERS_REQUIRED"),
        ("stop_loss", None, "DEMO_EXECUTION_PARAMETERS_REQUIRED"),
        ("take_profit", None, "DEMO_EXECUTION_PARAMETERS_REQUIRED"),
    ],
)
def test_missing_explicit_execution_values_fail_closed(tmp_path, field, value, code) -> None:
    _service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _validated(tmp_path)
    intent = values["intent"].model_copy(update={field: value})
    outcome = _validate(workflow, authorization, values, intent=intent)
    assert outcome.status is DemoDryRunStatus.REJECTED
    assert code in outcome.reason_codes
    assert state_store.count == 0 and adapter.calls == 0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"proposal": "proposal"}, "DEMO_AUTHORIZATION_EXACT_LINEAGE_MISMATCH"),
        ({"decision": "decision"}, "DEMO_SENTINEL_APPROVAL_INVALID"),
        ({"intent": "intent"}, "DEMO_EXECUTION_INTENT_LINEAGE_MISMATCH"),
        ({"context": "context"}, "DEMO_SYMBOL_MISMATCH"),
    ],
)
def test_exact_lineage_binding_mutations_fail_closed(tmp_path, change, code) -> None:
    _service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _validated(tmp_path)
    if change == {"proposal": "proposal"}:
        change = {"proposal": values["proposal"].model_copy(update={"proposal_id": "other-proposal"})}
    elif change == {"decision": "decision"}:
        change = {"decision": values["decision"].model_copy(update={"assessment_id": "other-assessment"})}
    elif change == {"intent": "intent"}:
        change = {"intent": values["intent"].model_copy(update={"analysis_id": "other-analysis"})}
    else:
        change = {"context": _context(instrument=_instrument(broker_symbol="XAUUSD.a"))}
    outcome = _validate(workflow, authorization, values, **change)
    assert outcome.status is DemoDryRunStatus.REJECTED
    assert code in outcome.reason_codes
    assert state_store.count == 0 and adapter.calls == 0


def test_expiry_kill_switch_risk_freshness_and_duplicate_state_fail_closed(tmp_path) -> None:
    service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _validated(tmp_path)
    expired = _validate(workflow, authorization, values, now=authorization.expires_at)
    assert "DEMO_AUTHORIZATION_EXPIRED" in expired.reason_codes

    # Use independent synthetic setups because a failed verification revokes
    # the tested authorization by design.
    service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _fresh(tmp_path, "kill")
    service._kill_switch.trigger(reason="synthetic", changed_by="test")
    assert "KILL_SWITCH_ACTIVE" in _validate(workflow, authorization, values).reason_codes

    service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _fresh(tmp_path, "stale")
    stale = _validate(workflow, authorization, values, context=_context(market_observed_at=NOW - timedelta(minutes=5)))
    assert "DEMO_MARKET_DATA_STALE" in stale.reason_codes

    service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _fresh(tmp_path, "duplicate")
    duplicate = build_execution_record(values["intent"], execution_mode="demo_controlled", now=NOW)
    state_store.create_or_get(duplicate)
    repeated = _validate(workflow, authorization, values)
    assert "DEMO_DUPLICATE_EXECUTION" in repeated.reason_codes
    assert state_store.count == 1 and adapter.calls == 0


def test_authorization_lifecycle_and_reconciliation_block(tmp_path) -> None:
    service, store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _validated(tmp_path)
    store.consume_for_future_submission(authorization_id=authorization.authorization_id, now=NOW)
    consumed = _validate(workflow, authorization, values)
    assert "DEMO_AUTHORIZATION_CONSUMED" in consumed.reason_codes

    service, store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _fresh(tmp_path, "revoked")
    store.revoke(authorization_id=authorization.authorization_id, reason_code="synthetic", now=NOW)
    revoked = _validate(workflow, authorization, values)
    assert "DEMO_AUTHORIZATION_REVOKED" in revoked.reason_codes

    service, _store, _registry, _audit, state_store, values, authorization, workflow, adapter, _result = _fresh(tmp_path, "reconciliation")
    record, _ = state_store.create_or_get(build_execution_record(values["intent"], execution_mode="demo_controlled", now=NOW))
    for state in (ExecutionLifecycleState.VALIDATED, ExecutionLifecycleState.READY_FOR_SUBMISSION, ExecutionLifecycleState.RECONCILIATION_REQUIRED):
        record = state_store.transition(execution_id=record.execution_id, to_state=state, now=NOW)
    blocked = _validate(workflow, authorization, values)
    assert "DEMO_EXECUTION_STATE_INVALID" in blocked.reason_codes
    assert adapter.calls == 0


def test_audit_failure_is_fail_closed_and_result_is_immutable(tmp_path) -> None:
    service, _store, registry, _audit, state_store, values, authorization, _workflow_, _adapter, _result = _validated(tmp_path)
    gate = FinalExecutionGate(
        approval_registry=registry, state_store=state_store, kill_switch=service._kill_switch, adapter=None, audit_service=None
    )
    no_audit = ControlledDemoDryRunWorkflow(
        authorization_service=service, final_execution_gate=gate, audit_service=None
    )
    outcome = _validate(no_audit, authorization, values)
    assert outcome.status is DemoDryRunStatus.REJECTED
    assert outcome.reason_codes == ("AUDIT_PERSISTENCE_UNAVAILABLE",)
    with pytest.raises((ValidationError, TypeError)):
        outcome.status = DemoDryRunStatus.VALIDATED


@pytest.mark.parametrize(
    "unsafe_flag",
    ("submission_attempted", "submission_allowed", "execution_state_mutated"),
)
def test_dry_run_result_rejects_each_unsafe_safety_flag(unsafe_flag: str) -> None:
    with pytest.raises(ValidationError, match="exactly False"):
        ControlledDemoDryRunResult(
            status=DemoDryRunStatus.VALIDATED,
            evaluated_at=NOW,
            **{unsafe_flag: True},
        )


def test_dry_run_result_accepts_only_safe_flags_and_remains_immutable() -> None:
    result = ControlledDemoDryRunResult(
        status=DemoDryRunStatus.VALIDATED,
        evaluated_at=NOW,
        submission_attempted=False,
        submission_allowed=False,
        execution_state_mutated=False,
    )
    assert result.submission_attempted is False
    assert result.submission_allowed is False
    assert result.execution_state_mutated is False
    with pytest.raises((ValidationError, TypeError)):
        result.submission_allowed = False


def test_static_safety_and_audit_semantics(tmp_path) -> None:
    _service, _store, _registry, audit, _state, _values, _authorization, _workflow_, _adapter, result = _validated(tmp_path)
    source = inspect.getsource(ControlledDemoDryRunWorkflow)
    assert "PilotExecutionCoordinator" not in source
    assert "order_send" not in source
    assert ".submit(" not in source
    assert all("submitted" not in event.state and "filled" not in event.state for event in audit.events())
    assert all(word not in repr((result, audit.events())).lower() for word in ("password", "api_key", "secret", "token"))
    assert result.proposal_fingerprint == proposal_fingerprint(_values["proposal"])
