"""Deterministic tests for non-authorizing Sentinel portfolio-risk evidence."""
from __future__ import annotations

from app.models.sentinel_risk_evidence import SentinelRiskEvidenceStatus
from app.services.sentinel_risk_evidence import SentinelPortfolioRiskEvidenceVerifier
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService
from tests.test_demo_execution_readiness import NOW, _pair, _policy
from tests.test_trusted_execution_evidence import _FakeProvider, _request, _response


def _inputs():
    proposal, decision, _registry, intent = _pair()
    policy = _policy()
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
    result = SentinelPortfolioRiskEvidenceVerifier().verify(
        policy=policy, proposal=proposal, decision=decision, intent=intent, evidence=evidence, now=NOW
    )
    assert result.status is SentinelRiskEvidenceStatus.VALIDATED
    assert result.execution_authority is False
    assert result.candidate_monetary_risk is not None and result.order_notional is not None


def test_policy_or_stale_evidence_mismatch_fails_closed() -> None:
    policy, proposal, decision, intent, evidence = _inputs()
    bad_policy = policy.model_copy(update={"policy_id": "other-policy"})
    result = SentinelPortfolioRiskEvidenceVerifier().verify(
        policy=bad_policy, proposal=proposal, decision=decision, intent=intent, evidence=evidence, now=NOW
    )
    assert result.status is SentinelRiskEvidenceStatus.REJECTED
    assert "SENTINEL_RISK_EVIDENCE_POLICY_MISMATCH" in result.reason_codes
