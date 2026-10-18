"""Phase 13H external-trust contracts. All providers and facts are synthetic."""

from __future__ import annotations

from datetime import timedelta
import inspect

import pytest
from pydantic import ValidationError

from app.models.execution_state import BrokerOrderObservation
from app.models.independent_governance import (
    BrokerReconciliationAssessment,
    BrokerReconciliationObservation,
    BrokerReconciliationRequest,
    ExternalAssessmentStatus,
    ExternalAuthorityAvailability,
    IndependentPolicyAuthorityAssessment,
    IndependentPolicyAuthorityObservation,
    IndependentPolicyAuthorityRequest,
)
from app.services import independent_governance as integration_module
from app.services.independent_governance import (
    BrokerReconciliationIntegration,
    IndependentPolicyAuthorityIntegration,
)
from tests.test_demo_execution_readiness import NOW, _policy
from tests.test_sentinel_policy_governance import _governance, _signed_state


def _authority_request(policy, **changes):
    values = {
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "policy_fingerprint": policy.fingerprint,
        "account_id": policy.allowed_account.account_id,
        "execution_mode": policy.mode.value,
        "canonical_symbol": policy.instrument.canonical_symbol,
        "risk_currency": policy.risk_currency,
        "challenge_id": "challenge-authority-0000000000000001",
        "requested_at": NOW - timedelta(seconds=1),
        "max_age_seconds": 30,
    }
    values.update(changes)
    return IndependentPolicyAuthorityRequest(**values)


def _authority_observation(request, policy, **changes):
    values = {
        "availability": ExternalAuthorityAvailability.AVAILABLE,
        "provider_id": "SYNTHETIC-INDEPENDENT-AUTHORITY",
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "source_event_id": "synthetic-governance-event-1",
        "source_sequence": 1,
        "observed_at": NOW - timedelta(seconds=2),
        "retrieved_at": NOW,
        "governance": _governance(policy),
        "approval": _signed_state(policy),
        "trust_anchor_generation": 1,
        "audit_reference": "synthetic-audit-record-1",
    }
    values.update(changes)
    return IndependentPolicyAuthorityObservation(**values)


class _AuthorityProvider:
    provider_id = "SYNTHETIC-INDEPENDENT-AUTHORITY"

    def __init__(self, observation):
        self.observation = observation

    def observe(self, request):
        return self.observation


class _BrokenAuthorityProvider:
    @property
    def provider_id(self):
        raise RuntimeError("synthetic provider outage")

    def observe(self, request):
        raise RuntimeError("synthetic provider outage")


def _broker_request(**changes):
    values = {
        "execution_id": "execution-synthetic-1",
        "execution_record_fingerprint": "a" * 64,
        "attempt_identity": "b" * 64,
        "account_id": "demo-123",
        "account_reference": "Demo-Server",
        "canonical_symbol": "XAUUSD",
        "broker_symbol": "XAUUSD",
        "broker_order_id": "broker-order-1",
        "challenge_id": "challenge-broker-000000000000000001",
        "requested_at": NOW - timedelta(seconds=1),
        "max_age_seconds": 30,
    }
    values.update(changes)
    return BrokerReconciliationRequest(**values)


def _broker_order(request, **changes):
    values = {
        "broker_order_id": request.broker_order_id,
        "canonical_symbol": request.canonical_symbol,
        "broker_symbol": request.broker_symbol,
        "direction": "LONG",
        "requested_quantity": 0.01,
        "filled_quantity": 0.01,
        "remaining_quantity": 0.0,
        "requested_price": 2300.0,
        "actual_price": 2300.0,
        "stop_loss": 2290.0,
        "take_profit": 2320.0,
        "status": "FILLED",
        "account_id": request.account_id,
    }
    values.update(changes)
    return BrokerOrderObservation(**values)


def _broker_observation(request, **changes):
    values = {
        "availability": ExternalAuthorityAvailability.AVAILABLE,
        "provider_id": "SYNTHETIC-BROKER-READER",
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "source_event_id": "synthetic-broker-event-1",
        "source_sequence": 7,
        "account_reference": request.account_reference,
        "observed_at": NOW - timedelta(seconds=2),
        "retrieved_at": NOW,
        "broker_order": _broker_order(request),
        "audit_reference": "synthetic-broker-audit-1",
    }
    values.update(changes)
    return BrokerReconciliationObservation(**values)


class _BrokerProvider:
    provider_id = "SYNTHETIC-BROKER-READER"

    def __init__(self, observation):
        self.observation = observation

    def observe(self, request):
        return self.observation


def test_default_policy_authority_is_unavailable_and_non_authorizing() -> None:
    policy = _policy()
    result = IndependentPolicyAuthorityIntegration().assess(
        request=_authority_request(policy), policy=policy, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.UNAVAILABLE
    assert result.independently_authoritative is False
    assert result.production_authorization_available is False
    assert result.execution_authority is False


def test_policy_authority_provider_outage_fails_closed() -> None:
    policy = _policy()
    result = IndependentPolicyAuthorityIntegration(_BrokenAuthorityProvider()).assess(
        request=_authority_request(policy), policy=policy, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.UNAVAILABLE
    assert result.reason_codes == ("INDEPENDENT_POLICY_AUTHORITY_PROVIDER_ERROR",)
    assert result.execution_authority is False


def test_structurally_valid_policy_observation_remains_non_authoritative() -> None:
    policy = _policy()
    request = _authority_request(policy)
    observation = _authority_observation(request, policy)
    result = IndependentPolicyAuthorityIntegration(_AuthorityProvider(observation)).assess(
        request=request, policy=policy, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.STRUCTURALLY_VALIDATED
    assert result.observation_fingerprint == observation.observation_fingerprint
    assert result.independently_authoritative is False
    assert result.production_authorization_available is False
    assert result.execution_authority is False


@pytest.mark.parametrize(
    "field", ("independently_authoritative", "production_authorization_available", "execution_authority")
)
def test_authority_assessment_rejects_any_authorizing_flag(field: str) -> None:
    values = {
        "status": ExternalAssessmentStatus.UNAVAILABLE,
        "request_fingerprint": "a" * 64,
        "provider_id": "provider",
        "evaluated_at": NOW,
        "reason_codes": ("SOURCE_UNAVAILABLE",),
        field: True,
    }
    with pytest.raises(ValidationError):
        IndependentPolicyAuthorityAssessment(**values)


def test_policy_observation_rejects_mismatched_challenge_and_stale_facts() -> None:
    policy = _policy()
    request = _authority_request(policy)
    observation = _authority_observation(
        request,
        policy,
        challenge_id="different-challenge-0000000000000001",
        observed_at=NOW - timedelta(minutes=5),
    )
    result = IndependentPolicyAuthorityIntegration(_AuthorityProvider(observation)).assess(
        request=request, policy=policy, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.STALE
    assert "INDEPENDENT_POLICY_AUTHORITY_CHALLENGE_MISMATCH" in result.reason_codes
    assert "EXTERNAL_OBSERVATION_STALE" in result.reason_codes


def test_policy_observation_enforces_explicit_key_rotation_lineage() -> None:
    policy = _policy()
    request = _authority_request(policy)
    with pytest.raises(ValidationError):
        _authority_observation(request, policy, trust_anchor_generation=2)
    rotated = _authority_observation(
        request,
        policy,
        trust_anchor_generation=2,
        previous_trust_anchor_fingerprint="b" * 64,
    )
    assert rotated.previous_trust_anchor_fingerprint == "b" * 64


def test_authority_request_and_observation_are_immutable_and_deterministic() -> None:
    policy = _policy()
    first = _authority_request(policy)
    second = _authority_request(policy)
    assert first.request_fingerprint == second.request_fingerprint
    observation = _authority_observation(first, policy)
    with pytest.raises(ValidationError):
        observation.provider_id = "changed"  # type: ignore[misc]


def test_default_broker_reconciliation_is_unavailable_and_never_allows_retry() -> None:
    result = BrokerReconciliationIntegration().assess(
        request=_broker_request(), evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.UNAVAILABLE
    assert result.authoritative_reconciliation_established is False
    assert result.retry_allowed is False
    assert result.execution_authority is False


def test_structurally_valid_broker_observation_is_exactly_bound_but_non_authorizing() -> None:
    request = _broker_request()
    observation = _broker_observation(request)
    result = BrokerReconciliationIntegration(_BrokerProvider(observation)).assess(
        request=request, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.STRUCTURALLY_VALIDATED
    assert result.broker_order == observation.broker_order
    assert result.authoritative_reconciliation_established is False
    assert result.retry_allowed is False
    assert result.execution_authority is False


def test_broker_reconciliation_fails_closed_on_account_order_symbol_and_freshness() -> None:
    request = _broker_request()
    observation = _broker_observation(
        request,
        account_reference="Other-Server",
        observed_at=NOW - timedelta(minutes=5),
        broker_order=_broker_order(
            request,
            account_id="other-account",
            broker_order_id="other-order",
            canonical_symbol="EURUSD",
            broker_symbol="EURUSD",
        ),
    )
    result = BrokerReconciliationIntegration(_BrokerProvider(observation)).assess(
        request=request, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.STALE
    assert {
        "BROKER_RECONCILIATION_ACCOUNT_REFERENCE_MISMATCH",
        "BROKER_RECONCILIATION_ACCOUNT_MISMATCH",
        "BROKER_RECONCILIATION_ORDER_ID_MISMATCH",
        "BROKER_RECONCILIATION_SYMBOL_MISMATCH",
        "BROKER_RECONCILIATION_BROKER_SYMBOL_MISMATCH",
        "EXTERNAL_OBSERVATION_STALE",
    }.issubset(result.reason_codes)


def test_confirmed_absence_is_provenance_bound_but_not_authoritative() -> None:
    request = _broker_request()
    observation = _broker_observation(
        request,
        availability=ExternalAuthorityAvailability.NOT_FOUND,
        broker_order=None,
    )
    result = BrokerReconciliationIntegration(_BrokerProvider(observation)).assess(
        request=request, evaluated_at=NOW
    )
    assert result.status is ExternalAssessmentStatus.CONFIRMED_ABSENT
    assert result.authoritative_reconciliation_established is False
    assert result.retry_allowed is False


@pytest.mark.parametrize(
    "field", ("authoritative_reconciliation_established", "retry_allowed", "execution_authority")
)
def test_broker_assessment_rejects_authorizing_or_retry_flags(field: str) -> None:
    values = {
        "status": ExternalAssessmentStatus.UNAVAILABLE,
        "request_fingerprint": "a" * 64,
        "provider_id": "provider",
        "evaluated_at": NOW,
        "reason_codes": ("SOURCE_UNAVAILABLE",),
        field: True,
    }
    with pytest.raises(ValidationError):
        BrokerReconciliationAssessment(**values)


def test_integration_module_has_no_submission_or_mt5_reachability() -> None:
    source = inspect.getsource(integration_module)
    prohibited = (
        "MetaTrader5",
        "order_send",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "ExecutionStateStore",
        "reconcile(",
    )
    assert all(token not in source for token in prohibited)
