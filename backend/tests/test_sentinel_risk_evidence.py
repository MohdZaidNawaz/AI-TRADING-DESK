"""Deterministic tests for non-authorizing Sentinel portfolio-risk evidence."""
from __future__ import annotations

from datetime import timedelta

from app.models.sentinel_risk_evidence import SentinelRiskEvidenceStatus
from app.services.sentinel_risk_evidence import SentinelPortfolioRiskEvidenceVerifier
from app.services.sentinel_policy_authority import SentinelPortfolioRiskPolicyVerifier
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService
from tests.test_demo_execution_readiness import NOW, _pair, _policy
from tests.test_sentinel_policy_authority import _test_policy_record
from tests.test_trusted_execution_evidence import _FakeProvider, _request, _response


def _inputs():
    proposal, decision, _registry, intent = _pair()
    policy = _policy(
        effective_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )
    request = _request(
        account=policy.allowed_account,
        canonical_symbol=policy.instrument.canonical_symbol,
        broker_symbol=policy.instrument.broker_symbol,
        expected_market_unit=policy.risk_currency,
        required_order_type=intent.order_type,
        required_filling_mode=intent.filling_mode,
        required_risk_policy_id=policy.policy_id,
        required_risk_policy_fingerprint=policy.fingerprint,
    )
    base = _response()
    response = _response(
        observed_at=NOW,
        account=base.account.model_copy(update={"account": policy.allowed_account}),
        market=base.market.model_copy(update={"canonical_symbol": "XAUUSD", "broker_symbol": "XAUUSD", "observed_at": NOW}),
        instrument=base.instrument.model_copy(update={
            "canonical_symbol": "XAUUSD", "broker_symbol": "XAUUSD",
            "tick_value": 1.0, "contract_size": 100.0,
        }),
        portfolio_risk_inputs=base.portfolio_risk_inputs.model_copy(
            update={"account": policy.allowed_account, "policy_id": policy.policy_id, "policy_fingerprint": policy.fingerprint, "observed_at": NOW}
        ),
    )
    evidence = TrustedExecutionEvidenceService(_FakeProvider(response)).assess(request, now=NOW)
    assert evidence.ready, evidence.reason_codes
    return policy, proposal, decision, intent, evidence


def test_recomputes_policy_metrics_from_validated_evidence_without_authorizing() -> None:
    policy, proposal, decision, intent, evidence = _inputs()
    result = SentinelPortfolioRiskEvidenceVerifier(
        SentinelPortfolioRiskPolicyVerifier.for_test((_test_policy_record(policy),))
    ).verify(
        policy=policy, proposal=proposal, decision=decision, intent=intent, evidence=evidence, now=NOW
    )
    assert result.status is SentinelRiskEvidenceStatus.VALIDATED
    assert result.execution_authority is False
    assert result.candidate_monetary_risk is not None and result.order_notional is not None


def test_policy_or_stale_evidence_mismatch_fails_closed() -> None:
    policy, proposal, decision, intent, evidence = _inputs()
    bad_policy = policy.model_copy(update={"policy_id": "other-policy"})
    result = SentinelPortfolioRiskEvidenceVerifier(
        SentinelPortfolioRiskPolicyVerifier.for_test((_test_policy_record(policy),))
    ).verify(
        policy=bad_policy, proposal=proposal, decision=decision, intent=intent, evidence=evidence, now=NOW
    )
    assert result.status is SentinelRiskEvidenceStatus.UNAVAILABLE
    assert "SENTINEL_RISK_POLICY_VERSION_UNAVAILABLE" in result.reason_codes


def test_default_policy_authority_is_unavailable_and_metrics_are_not_computed() -> None:
    policy, proposal, decision, intent, evidence = _inputs()
    result = SentinelPortfolioRiskEvidenceVerifier().verify(
        policy=policy, proposal=proposal, decision=decision, intent=intent, evidence=evidence, now=NOW
    )
    assert result.status is SentinelRiskEvidenceStatus.UNAVAILABLE
    assert result.reason_codes == ("SENTINEL_RISK_POLICY_AUTHORITY_UNAVAILABLE",)
    assert result.candidate_monetary_risk is None
    assert result.order_notional is None
    assert result.execution_authority is False


def test_caller_cannot_override_source_resolved_risk_limits() -> None:
    policy, proposal, decision, intent, evidence = _inputs()
    source_record = _test_policy_record(policy)
    altered = policy.model_copy(update={"max_order_notional": policy.max_order_notional + 1})
    result = SentinelPortfolioRiskEvidenceVerifier(
        SentinelPortfolioRiskPolicyVerifier.for_test((source_record,))
    ).verify(
        policy=altered, proposal=proposal, decision=decision, intent=intent, evidence=evidence, now=NOW
    )
    assert result.status is SentinelRiskEvidenceStatus.UNAVAILABLE
    assert "SENTINEL_RISK_POLICY_CONTENT_MISMATCH" in result.reason_codes
