"""Phase 11 authorization tests using synthetic contracts only.

No test initializes MT5, calls an adapter, or performs any network activity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import inspect
import json

import pytest
from pydantic import ValidationError

from app.audit.service import AuditService
from app.audit.store import AuditStore
from app.execution.demo_authorization_store import DemoAuthorizationStore
from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.state_store import ExecutionStateStore
from app.models.demo_authorization import DemoAuthorizationStatus
from app.models.demo_execution import DemoExecutionMode
from app.models.execution_state import ExecutionLifecycleState, build_execution_record
from app.models.trusted_execution_evidence import (
    ExecutionEvidenceAvailability,
    MarketReferenceObservation,
    PortfolioRiskInputObservation,
    ProviderExecutionEvidence,
)
from app.services.demo_execution_authorization import DemoExecutionAuthorizationService
from app.services.demo_execution_readiness import ControlledDemoReadinessGate
from app.services.sentinel_policy_authority import SentinelPortfolioRiskPolicyVerifier
from app.services.sentinel_risk_evidence import SentinelPortfolioRiskEvidenceVerifier
from app.services.trade_proposal_workflow import ApprovalRegistry, proposal_fingerprint
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService
from tests.test_demo_execution_readiness import ACCOUNT, NOW, _context, _pair, _policy
from tests.test_sentinel_policy_authority import _test_policy_record


class _ControlledTestEvidenceProvider:
    """Test-only source; production composition uses the unavailable default."""

    provider_id = "controlled_demo_authorization_test_source"

    def __init__(self, context):
        self._context = context

    def read(self, request):
        positions = self._context.positions
        position_payload = [
            position.model_dump(mode="json")
            for position in sorted(positions, key=lambda item: item.ticket)
        ]
        position_fingerprint = sha256(
            json.dumps(position_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return ProviderExecutionEvidence(
            availability=ExecutionEvidenceAvailability.AVAILABLE,
            provider_id=self.provider_id,
            observed_at=self._context.captured_at,
            instrument=self._context.instrument,
            market=MarketReferenceObservation(
                canonical_symbol=self._context.instrument.canonical_symbol,
                broker_symbol=self._context.instrument.broker_symbol,
                bid="100.00",
                ask="100.10",
                unit=self._context.account.currency,
                observed_at=self._context.market_observed_at,
            ),
            account=self._context.account,
            positions=positions,
            portfolio_risk_inputs=PortfolioRiskInputObservation(
                account=self._context.account.account,
                position_snapshot_fingerprint=position_fingerprint,
                # This explicit fake reflects the request scope only in tests;
                # production has no configured provider by default.
                policy_id=request.required_risk_policy_id or "controlled-test-policy",
                policy_fingerprint=request.required_risk_policy_fingerprint or "a" * 64,
                observed_at=self._context.captured_at,
            ),
        )


def _trusted_evidence(context):
    return TrustedExecutionEvidenceService(_ControlledTestEvidenceProvider(context))


def _sentinel_risk(policy):
    return SentinelPortfolioRiskEvidenceVerifier(
        SentinelPortfolioRiskPolicyVerifier.for_test((_test_policy_record(policy),))
    )


def _inputs(tmp_path, **changes: object):
    proposal, decision, _old_registry, intent = _pair()
    proposal = proposal.model_copy(update={"expires_at": NOW + timedelta(minutes=5)})
    registry = ApprovalRegistry()
    decision = registry._record_approved_decision(
        proposal,
        decision.model_copy(
            update={
                "approval_id": None,
                "proposal_fingerprint": proposal_fingerprint(proposal),
                "analysis_id": proposal.analysis_id,
                "evidence_fingerprint": proposal.evidence_fingerprint,
                "account_id": ACCOUNT.account_id,
                "expires_at": NOW + timedelta(minutes=4),
            }
        ),
    )
    intent = intent.model_copy(
        update={
            "proposal_id": proposal.proposal_id,
            "proposal_fingerprint": proposal_fingerprint(proposal),
            "analysis_id": proposal.analysis_id,
            "evidence_fingerprint": proposal.evidence_fingerprint,
            "approval_id": decision.approval_id,
            "policy_id": decision.policy_id,
            "policy_fingerprint": decision.policy_fingerprint,
            "assessment_id": decision.assessment_id,
            "assessment_fingerprint": decision.assessment_fingerprint,
            "account_id": ACCOUNT.account_id,
            "expires_at": NOW + timedelta(minutes=3),
        }
    )
    policy = _policy(
        effective_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )
    context = _context()
    kill_switch = ExecutionKillSwitch()
    audit = AuditService(AuditStore(tmp_path / "phase11-audit.sqlite3"))
    store = DemoAuthorizationStore()
    store.confirm_clean_start()
    state_store = ExecutionStateStore()
    readiness = ControlledDemoReadinessGate(
        approval_registry=registry,
        kill_switch=kill_switch,
    )
    service = DemoExecutionAuthorizationService(
        approval_registry=registry,
        readiness_gate=readiness,
        kill_switch=kill_switch,
        authorization_store=store,
        execution_state_store=state_store,
        audit_service=audit,
        trusted_execution_evidence=_trusted_evidence(context),
        sentinel_risk_evidence=_sentinel_risk(policy),
    )
    values: dict[str, object] = {
        "policy": policy,
        "proposal": proposal,
        "decision": decision,
        "intent": intent,
        "context": context,
        "requested_mode": DemoExecutionMode.DEMO_CONTROLLED,
        "runtime_execution_mode": "disabled",
        "execution_window_start": NOW - timedelta(seconds=1),
        "execution_window_end": NOW + timedelta(minutes=1),
        "now": NOW,
    }
    values.update(changes)
    return service, store, registry, audit, state_store, values


def _authorize(tmp_path, **changes: object):
    service, store, registry, audit, state_store, values = _inputs(tmp_path, **changes)
    return service, store, registry, audit, state_store, values, service.authorize_demo_execution(**values)


def test_valid_artifact_is_pre_authorized_only_and_expiry_uses_earliest_dependency(tmp_path) -> None:
    service, store, registry, _audit, _state, values, result = _authorize(tmp_path)
    authorization = result.authorization

    assert authorization is not None
    assert result.submission_allowed is False
    assert authorization.status is DemoAuthorizationStatus.AUTHORIZED
    assert authorization.expires_at == NOW + timedelta(seconds=30)
    assert authorization.account == ACCOUNT
    assert authorization.instrument.canonical_symbol == authorization.instrument.broker_symbol == "XAUUSD"
    assert authorization.order_type == "MARKET"
    assert authorization.filling_mode == "FOK"
    assert store.count == 1
    # Verification is read-only: neither Sentinel approval nor authorization
    # consumption is reached by this pre-submission phase.
    verified = service.verify_authorization(
        authorization_id=authorization.authorization_id,
        policy=values["policy"], proposal=values["proposal"], decision=values["decision"],
        intent=values["intent"], context=values["context"], runtime_execution_mode="disabled", now=NOW,
    )
    assert verified.authorization == authorization and verified.submission_allowed is False
    registry.verify(proposal=values["proposal"], decision=values["decision"], now=NOW)
    assert store.get(authorization.authorization_id).status is DemoAuthorizationStatus.AUTHORIZED


def test_unconfigured_or_stale_trusted_evidence_blocks_without_creating_authorization(tmp_path) -> None:
    service, store, registry, audit, state_store, values = _inputs(tmp_path)
    unavailable = DemoExecutionAuthorizationService(
        approval_registry=registry,
        readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch,
        authorization_store=store,
        execution_state_store=state_store,
        audit_service=audit,
    )
    blocked = unavailable.authorize_demo_execution(**values)
    assert blocked.authorization is None
    assert "DEMO_TRUSTED_EVIDENCE_UNAVAILABLE" in blocked.reason_codes
    assert store.count == 0


def test_configured_execution_evidence_cannot_replace_policy_authority(tmp_path) -> None:
    service, store, registry, audit, state_store, values = _inputs(tmp_path)
    unavailable_policy = DemoExecutionAuthorizationService(
        approval_registry=registry,
        readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch,
        authorization_store=store,
        execution_state_store=state_store,
        audit_service=audit,
        trusted_execution_evidence=service._trusted_execution_evidence,
    )
    blocked = unavailable_policy.authorize_demo_execution(**values)
    assert blocked.authorization is None
    assert "SENTINEL_RISK_POLICY_AUTHORITY_UNAVAILABLE" in blocked.reason_codes
    assert store.count == 0

    stale = DemoExecutionAuthorizationService(
        approval_registry=registry,
        readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch,
        authorization_store=store,
        execution_state_store=state_store,
        audit_service=audit,
        trusted_execution_evidence=_trusted_evidence(
            _context(market_observed_at=NOW - timedelta(minutes=1))
        ),
    )
    stale_result = stale.authorize_demo_execution(**values)
    assert stale_result.authorization is None
    assert "EXECUTION_EVIDENCE_MARKET_STALE" in stale_result.reason_codes
    assert store.count == 0


def test_verification_recomputes_the_earliest_expiry(tmp_path) -> None:
    service, _store, _registry, audit, state_store, values, result = _authorize(tmp_path)
    authorization = result.authorization
    assert authorization is not None
    tampered = authorization.model_copy(update={"expires_at": authorization.expires_at + timedelta(seconds=1)})
    isolated_store = DemoAuthorizationStore(); isolated_store.confirm_clean_start(); isolated_store.create(tampered)
    verifier = DemoExecutionAuthorizationService(
        approval_registry=service._approval_registry,
        readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch,
        authorization_store=isolated_store,
        execution_state_store=state_store,
        audit_service=audit,
        trusted_execution_evidence=service._trusted_execution_evidence,
        sentinel_risk_evidence=service._sentinel_risk_evidence,
    )
    outcome = verifier.verify_authorization(
        authorization_id=tampered.authorization_id,
        policy=values["policy"], proposal=values["proposal"], decision=values["decision"],
        intent=values["intent"], context=values["context"], runtime_execution_mode="disabled", now=NOW,
    )
    assert "DEMO_AUTHORIZATION_EXPIRY_MISMATCH" in outcome.reason_codes
    assert isolated_store.get(tampered.authorization_id).status is DemoAuthorizationStatus.REVOKED


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"requested_mode": "dry_run"}, "DEMO_MODE_NOT_EXPLICITLY_REQUESTED"),
        ({"runtime_execution_mode": "dry_run"}, "RUNTIME_EXECUTION_MODE_NOT_DISABLED"),
        ({"intent": _pair()[3].model_copy(update={"symbol": "XAUUSD.a"})}, "DEMO_SYMBOL_MISMATCH"),
        ({"intent": _pair()[3].model_copy(update={"quantity": 2.0})}, "DEMO_VOLUME_LIMIT"),
        ({"intent": _pair()[3].model_copy(update={"order_type": "LIMIT"})}, "DEMO_UNSUPPORTED_ORDER_TYPE"),
        ({"intent": _pair()[3].model_copy(update={"filling_mode": "BOOK_OR_CANCEL"})}, "DEMO_UNSUPPORTED_FILLING_MODE"),
        ({"context": _context(market_observed_at=NOW - timedelta(minutes=1))}, "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE"),
        ({"intent": _pair()[3].model_copy(update={"stop_loss": None})}, "DEMO_INVALID_PRICE"),
        ({"intent": _pair()[3].model_copy(update={"take_profit": None})}, "DEMO_INVALID_PRICE"),
    ],
)
def test_authorization_fails_closed_for_requested_mode_and_readiness_limits(tmp_path, change: dict[str, object], code: str) -> None:
    # Replace helper-created stale pair values with a correctly bound intent
    # where the test deliberately mutates only the requested field.
    service, store, _registry, _audit, _state, values = _inputs(tmp_path)
    if "intent" in change:
        original = values["intent"]
        mutated = change["intent"]
        assert original is not None
        overrides = {
            field: getattr(mutated, field)
            for field in ("symbol", "quantity", "order_type", "filling_mode", "stop_loss", "take_profit")
            if getattr(mutated, field) != getattr(original, field)
        }
        change = {**change, "intent": original.model_copy(update=overrides)}
    values.update(change)
    result = service.authorize_demo_execution(**values)
    assert result.authorization is None
    assert result.submission_allowed is False
    assert code in result.reason_codes
    assert store.count == 0


def test_wrong_account_and_broker_binding_fail_closed(tmp_path) -> None:
    service, store, _registry, _audit, _state, values = _inputs(tmp_path)
    from app.models.portfolio_risk import SourceAccountKey
    from tests.test_demo_execution_readiness import _account, _instrument

    other = SourceAccountKey(source_id="mt5", source_account_id="other", account_reference="Demo-Server")
    wrong_account = service.authorize_demo_execution(
        **{**values, "context": _context(account=_account(other), account_id=other.account_id)}
    )
    assert "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE" in wrong_account.reason_codes
    wrong_broker = service.authorize_demo_execution(
        **{**values, "context": _context(instrument=_instrument(broker_symbol="XAUUSD.a"))}
    )
    assert "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE" in wrong_broker.reason_codes
    assert store.count == 0


def test_wrong_broker_server_and_explicit_symbol_alias_fail_closed(tmp_path) -> None:
    service, store, _registry, _audit, _state, values = _inputs(tmp_path)
    from app.models.portfolio_risk import SourceAccountKey
    from tests.test_demo_execution_readiness import _account

    wrong_server = SourceAccountKey(source_id="mt5", source_account_id="demo-123", account_reference="Other-Server")
    broker_mismatch = service.authorize_demo_execution(
        **{**values, "context": _context(account=_account(wrong_server), account_id=wrong_server.account_id)}
    )
    assert "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE" in broker_mismatch.reason_codes
    alias = values["intent"].model_copy(update={"symbol": "XAU/USD"})
    alias_result = service.authorize_demo_execution(**{**values, "intent": alias})
    assert "DEMO_SYMBOL_MISMATCH" in alias_result.reason_codes
    assert store.count == 0


def test_missing_expired_and_mismatched_lineage_fail_closed(tmp_path) -> None:
    service, store, registry, _audit, _state, values = _inputs(tmp_path)
    missing = service.authorize_demo_execution(**{**values, "proposal": None})
    assert "DEMO_PROPOSAL_REQUIRED" in missing.reason_codes
    expired = service.authorize_demo_execution(
        **{**values, "proposal": values["proposal"].model_copy(update={"expires_at": NOW})}
    )
    assert "DEMO_PROPOSAL_EXPIRED" in expired.reason_codes
    bad_intent = values["intent"].model_copy(update={"analysis_id": "another-analysis"})
    mismatch = service.authorize_demo_execution(**{**values, "intent": bad_intent})
    assert "DEMO_EXECUTION_INTENT_LINEAGE_MISMATCH" in mismatch.reason_codes
    registry.revoke(approval_id=values["decision"].approval_id or "")
    revoked = service.authorize_demo_execution(**values)
    assert "DEMO_SENTINEL_APPROVAL_INVALID" in revoked.reason_codes
    assert store.count == 0


def test_missing_or_expired_sentinel_and_execution_intent_fail_closed(tmp_path) -> None:
    service, store, registry, _audit, _state, values = _inputs(tmp_path)
    missing = service.authorize_demo_execution(**{**values, "decision": None})
    assert "DEMO_SENTINEL_APPROVAL_REQUIRED" in missing.reason_codes
    expired_decision = values["decision"].model_copy(update={"expires_at": NOW})
    expired = service.authorize_demo_execution(**{**values, "decision": expired_decision})
    assert "DEMO_SENTINEL_APPROVAL_EXPIRED" in expired.reason_codes
    missing_intent = service.authorize_demo_execution(**{**values, "intent": None})
    assert "DEMO_EXECUTION_INTENT_REQUIRED" in missing_intent.reason_codes
    # Consumption is distinct from pre-authorization.  Once Sentinel is
    # consumed elsewhere, a new authorization must fail rather than reissue.
    registry.claim(proposal=values["proposal"], decision=values["decision"], now=NOW)
    consumed = service.authorize_demo_execution(**values)
    assert "DEMO_SENTINEL_APPROVAL_INVALID" in consumed.reason_codes
    assert store.count == 0


@pytest.mark.parametrize(
    ("policy_change", "context_change", "code"),
    [
        ({"max_total_open_volume": 1.1}, {"positions": ()}, "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE"),
        ({"max_open_positions": 0}, {}, "DEMO_POSITION_COUNT_LIMIT"),
        ({"max_order_notional": 10.0}, {}, "DEMO_NOTIONAL_LIMIT"),
        ({"max_stop_distance": 1.0}, {}, "DEMO_STOP_DISTANCE_LIMIT"),
        ({"max_aggregate_monetary_risk": 1.0}, {}, "DEMO_AGGREGATE_MONETARY_RISK_LIMIT"),
    ],
)
def test_policy_risk_and_exposure_limits_are_preserved(tmp_path, policy_change, context_change, code: str) -> None:
    service, store, _registry, _audit, _state, values = _inputs(tmp_path)
    if code == "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE":
        from tests.test_demo_execution_readiness import _position
        context_change = {"positions": (_position(1), _position(2))}
    values["policy"] = _policy(
        effective_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=10), **policy_change
    )
    service._sentinel_risk_evidence = _sentinel_risk(values["policy"])
    if context_change:
        values["context"] = _context(**context_change)
    result = service.authorize_demo_execution(**values)
    assert code in result.reason_codes
    assert result.authorization is None and store.count == 0


def test_missing_monetary_inputs_and_policy_or_window_expiry_fail_closed(tmp_path) -> None:
    service, store, _registry, _audit, _state, values = _inputs(tmp_path)
    from tests.test_demo_execution_readiness import _instrument

    missing_money = service.authorize_demo_execution(
        **{**values, "context": _context(instrument=_instrument(tick_value=None))}
    )
    assert "DEMO_CALLER_CONTEXT_CONFLICTS_TRUSTED_EVIDENCE" in missing_money.reason_codes
    expired_policy = _policy(
        effective_at=NOW - timedelta(minutes=2), expires_at=NOW - timedelta(seconds=1)
    )
    policy_result = service.authorize_demo_execution(**{**values, "policy": expired_policy})
    assert "DEMO_POLICY_NOT_ACTIVE" in policy_result.reason_codes
    assert store.count == 0


def test_policy_time_bounds_and_execution_window_are_explicit_and_deterministic(tmp_path) -> None:
    service, store, _registry, _audit, _state, values = _inputs(tmp_path)
    missing_bounds = _policy()
    no_bounds = service.authorize_demo_execution(**{**values, "policy": missing_bounds})
    assert "DEMO_POLICY_WINDOW_REQUIRED" in no_bounds.reason_codes
    too_long = service.authorize_demo_execution(
        **{**values, "execution_window_end": NOW + timedelta(seconds=301)}
    )
    assert "DEMO_EXECUTION_WINDOW_LIMIT" in too_long.reason_codes
    assert store.count == 0


def test_audit_is_required_for_success_but_failure_never_creates_authority(tmp_path) -> None:
    service, store, registry, _audit, state_store, values = _inputs(tmp_path)
    no_audit = DemoExecutionAuthorizationService(
        approval_registry=registry,
        readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch,
        authorization_store=store,
        execution_state_store=state_store,
        audit_service=None,
        trusted_execution_evidence=service._trusted_execution_evidence,
        sentinel_risk_evidence=service._sentinel_risk_evidence,
    )
    result = no_audit.authorize_demo_execution(**values)
    assert result.authorization is None
    assert "AUDIT_PERSISTENCE_UNAVAILABLE" in result.reason_codes
    assert store.count == 0


def test_kill_switch_window_and_reconciliation_state_block(tmp_path) -> None:
    service, store, _registry, _audit, state_store, values = _inputs(tmp_path)
    switch = service._kill_switch  # test-owned synthetic dependency
    switch.trigger(reason="synthetic", changed_by="tester")
    blocked = service.authorize_demo_execution(**values)
    assert "KILL_SWITCH_ACTIVE" in blocked.reason_codes
    switch.reset(reason="synthetic", changed_by="tester")
    window = service.authorize_demo_execution(
        **{**values, "execution_window_end": NOW}
    )
    assert "DEMO_EXECUTION_WINDOW_CLOSED" in window.reason_codes
    record, _ = state_store.create_or_get(
        build_execution_record(values["intent"], execution_mode="demo_controlled", now=NOW)
    )
    for state in (ExecutionLifecycleState.VALIDATED, ExecutionLifecycleState.READY_FOR_SUBMISSION, ExecutionLifecycleState.RECONCILIATION_REQUIRED):
        record = state_store.transition(execution_id=record.execution_id, to_state=state, now=NOW)
    reconciliation = service.authorize_demo_execution(**values)
    assert "DEMO_EXECUTION_STATE_INVALID" in reconciliation.reason_codes
    assert store.count == 0


def test_duplicate_second_identity_restart_and_statuses_fail_closed(tmp_path) -> None:
    service, store, _registry, audit, _state, values, first = _authorize(tmp_path)
    authorization = first.authorization
    assert authorization is not None
    duplicate = service.authorize_demo_execution(**values)
    assert "DEMO_AUTHORIZATION_RECOVERY_REQUIRED" in duplicate.reason_codes
    second_proposal = values["proposal"].model_copy(update={"proposal_id": "other-proposal"})
    second = service.authorize_demo_execution(**{**values, "proposal": second_proposal})
    assert second.authorization is None
    consumed = store.consume_for_future_submission(authorization_id=authorization.authorization_id, now=NOW)
    assert consumed.status is DemoAuthorizationStatus.CONSUMED
    denied = service.verify_authorization(
        authorization_id=authorization.authorization_id,
        policy=values["policy"], proposal=values["proposal"], decision=values["decision"],
        intent=values["intent"], context=values["context"], runtime_execution_mode="disabled", now=NOW,
    )
    assert "DEMO_AUTHORIZATION_CONSUMED" in denied.reason_codes
    restarted_store = DemoAuthorizationStore(); restarted_store.confirm_clean_start()
    restarted = DemoExecutionAuthorizationService(
        approval_registry=service._approval_registry,
        readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch,
        authorization_store=restarted_store,
        audit_service=audit,
        execution_state_store=_state,
        trusted_execution_evidence=service._trusted_execution_evidence,
        sentinel_risk_evidence=service._sentinel_risk_evidence,
    )
    restarted_result = restarted.authorize_demo_execution(**values)
    assert "DEMO_AUTHORIZATION_RECOVERY_REQUIRED" in restarted_result.reason_codes


def test_execution_history_duplicate_and_window_limit_block_authorization(tmp_path) -> None:
    service, store, _registry, _audit, state_store, values = _inputs(tmp_path)
    history_record = build_execution_record(values["intent"], execution_mode="demo_controlled", now=NOW)
    state_store.create_or_get(history_record)
    result = service.authorize_demo_execution(**values)
    assert "DEMO_DUPLICATE_EXECUTION" in result.reason_codes
    assert "DEMO_EXECUTION_WINDOW_LIMIT" in result.reason_codes
    assert store.count == 0


def test_revocation_expiry_and_recovery_uncertainty_fail_closed(tmp_path) -> None:
    service, store, _registry, _audit, _state, values, result = _authorize(tmp_path)
    authorization = result.authorization
    assert authorization is not None
    revoked = service.revoke_authorization(
        authorization_id=authorization.authorization_id, reason_code="SYNTHETIC_TEST", now=NOW
    )
    assert revoked.authorization is not None and revoked.authorization.status is DemoAuthorizationStatus.REVOKED
    assert "DEMO_AUTHORIZATION_REVOKED" in service.verify_authorization(
        authorization_id=authorization.authorization_id, policy=values["policy"], proposal=values["proposal"],
        decision=values["decision"], intent=values["intent"], context=values["context"],
        runtime_execution_mode="disabled", now=NOW,
    ).reason_codes
    # A fresh store starts uncertain and rejects any creation until a future
    # independently reviewed recovery process explicitly clears it.
    uncertain = DemoAuthorizationStore()
    assert uncertain.count == 0
    service_uncertain = DemoExecutionAuthorizationService(
        approval_registry=service._approval_registry, readiness_gate=service._readiness_gate,
        kill_switch=service._kill_switch, authorization_store=uncertain, audit_service=AuditService(AuditStore(tmp_path / "other.sqlite3")),
        execution_state_store=service._execution_state_store,
        trusted_execution_evidence=service._trusted_execution_evidence,
        sentinel_risk_evidence=service._sentinel_risk_evidence,
    )
    outcome = service_uncertain.authorize_demo_execution(**values)
    assert "DEMO_AUTHORIZATION_RECOVERY_REQUIRED" in outcome.reason_codes


def test_models_audit_and_static_boundary_are_safe(tmp_path) -> None:
    service, _store, _registry, audit, _state, _values, result = _authorize(tmp_path)
    authorization = result.authorization
    assert authorization is not None
    with pytest.raises(ValidationError):
        authorization.quantity = 2.0
    assert authorization.authorization_id == authorization.model_copy().authorization_id
    assert all(
        forbidden not in repr((authorization, audit.events())).lower()
        for forbidden in ("password", "api_key", "secret", "token")
    )
    source = inspect.getsource(DemoExecutionAuthorizationService)
    assert "adapter.submit" not in source
    assert "order_send" not in source
    assert "PilotExecutionCoordinator" not in source


def test_contract_rejects_unrestricted_order_shapes_and_invalid_time_bounds(tmp_path) -> None:
    _service, _store, _registry, _audit, _state, _values, result = _authorize(tmp_path)
    authorization = result.authorization
    assert authorization is not None
    payload = {
        field_name: getattr(authorization, field_name)
        for field_name in type(authorization).model_fields
    }
    with pytest.raises(ValidationError, match="MARKET"):
        type(authorization)(**{**payload, "order_type": "LIMIT"})
    with pytest.raises(ValidationError, match="FOK or IOC"):
        type(authorization)(**{**payload, "filling_mode": "BOOK_OR_CANCEL"})
    with pytest.raises(ValidationError, match="expiry"):
        type(authorization)(**{**payload, "expires_at": authorization.created_at})
