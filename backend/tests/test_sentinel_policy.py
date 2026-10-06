"""Phase 7A tests for immutable, versioned Sentinel policy contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.proposals import RiskContext
from app.models.sentinel_policy import (
    MissingDataStrategy,
    SentinelPolicyMetadata,
    SentinelPolicyRules,
    SentinelRiskPolicy,
)
from app.services.sentinel_policy import policy_from_risk_context


MOMENT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _policy(**changes: object) -> SentinelRiskPolicy:
    values: dict[str, object] = {
        "policy_id": "test-sentinel-policy",
        "policy_version": "v1",
        "effective_at": MOMENT,
        "missing_data_strategy": MissingDataStrategy.INSUFFICIENT_DATA,
        "rules": SentinelPolicyRules(),
        "max_market_evidence_age_seconds": 600,
        "approval_ttl_seconds": 120,
        "max_requested_risk_fraction": 0.02,
        "required_agreeing_specialists": 2,
        "reject_conflicting_signals": True,
        "metadata": (SentinelPolicyMetadata(key="scope", value="existing_rules"),),
    }
    values.update(changes)
    return SentinelRiskPolicy(**values)


def test_policy_and_nested_rules_are_immutable() -> None:
    policy = _policy()

    with pytest.raises(ValidationError):
        policy.policy_version = "v2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        policy.rules.require_symbol = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        policy.metadata[0].value = "changed"  # type: ignore[misc]


def test_policy_fingerprint_is_deterministic_and_semantic() -> None:
    first = _policy()
    same = _policy()
    changed_limit = _policy(max_requested_risk_fraction=0.01)
    changed_rule = _policy(rules=SentinelPolicyRules(require_news_risk_assessment_when_news_present=False))

    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != changed_limit.fingerprint
    assert first.fingerprint != changed_rule.fingerprint


def test_policy_rejects_irrelevant_runtime_metadata_and_raw_payloads() -> None:
    values = _policy().model_dump(mode="python")
    values["received_at"] = MOMENT
    with pytest.raises(ValidationError):
        SentinelRiskPolicy(**values)

    with pytest.raises(ValidationError):
        SentinelPolicyMetadata(key="scope", value="existing_rules", payload={})  # type: ignore[call-arg]


def test_policy_fingerprint_ignores_risk_context_runtime_evaluation_time() -> None:
    context = RiskContext(
        as_of=MOMENT,
        max_market_evidence_age_seconds=600,
        approval_ttl_seconds=120,
        max_risk_fraction=0.02,
        required_agreeing_specialists=2,
        reject_conflicting_signals=True,
    )
    later_context = context.model_copy(update={"as_of": MOMENT + timedelta(hours=1)})

    assert policy_from_risk_context(context).fingerprint == policy_from_risk_context(
        later_context
    ).fingerprint


def test_policy_effective_interval_has_deterministic_boundaries() -> None:
    policy = _policy(expires_at=MOMENT + timedelta(minutes=10))

    assert policy.is_effective_at(MOMENT)
    assert policy.is_effective_at(MOMENT + timedelta(minutes=9, seconds=59))
    assert not policy.is_effective_at(MOMENT - timedelta(microseconds=1))
    assert not policy.is_effective_at(MOMENT + timedelta(minutes=10))


def test_policy_rejects_invalid_expiry_and_naive_evaluation_time() -> None:
    with pytest.raises(ValidationError):
        _policy(expires_at=MOMENT)
    with pytest.raises(ValueError):
        _policy().is_effective_at(datetime(2026, 10, 7, 12, 0))


def test_policy_missing_data_strategy_is_explicit_and_non_permissive() -> None:
    policy = _policy()
    assert policy.missing_data_strategy is MissingDataStrategy.INSUFFICIENT_DATA
    assert {item.value for item in MissingDataStrategy} == {"insufficient_data", "rejected"}


def test_policy_has_no_approval_execution_or_trade_instruction_fields() -> None:
    fields = set(SentinelRiskPolicy.model_fields)
    forbidden = {"approval_id", "execute", "direction", "entry", "stop_loss", "target", "sizing"}
    assert not fields.intersection(forbidden)
    assert not hasattr(_policy(), "approve")
    assert not hasattr(_policy(), "execute")
