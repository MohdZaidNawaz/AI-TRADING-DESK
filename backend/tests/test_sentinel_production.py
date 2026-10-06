"""Focused Phase 7B/7C tests for fail-closed internal Sentinel checks."""

from __future__ import annotations

from datetime import UTC, datetime

from app.evidence.models import EvidenceSnapshot
from app.models.risk_assessment import EventRiskStatus, RiskCheckStatus
from app.models.sentinel_policy import SentinelRiskPolicy
from app.services.sentinel_production import production_checks


MOMENT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _policy(**changes: object) -> SentinelRiskPolicy:
    values: dict[str, object] = {
        "policy_id": "phase7-production-test",
        "policy_version": "v1",
        "effective_at": MOMENT,
        "require_portfolio_metric_binding": True,
        "max_portfolio_evidence_age_seconds": 60,
        "max_portfolio_metrics_age_seconds": 60,
    }
    values.update(changes)
    return SentinelRiskPolicy(**values)


def test_missing_explicit_portfolio_binding_fails_closed() -> None:
    checks, _events = production_checks(
        policy=_policy(),
        snapshot=EvidenceSnapshot(),
        evaluated_at=MOMENT,
        account_binding=None,
    )
    result = next(item for item in checks if item.check_name == "portfolio_account_binding")
    assert result.status is RiskCheckStatus.INSUFFICIENT_DATA
    assert result.reason_codes == ("account_binding_missing",)


def test_event_requirements_use_immutable_evidence_or_fail_closed() -> None:
    checks, events = production_checks(
        policy=_policy(
            require_portfolio_metric_binding=False,
            require_news_event_assessment=True,
            require_economic_event_assessment=True,
            require_corporate_action_assessment=True,
        ),
        snapshot=EvidenceSnapshot(),
        evaluated_at=MOMENT,
        account_binding=None,
    )
    assert {item.status for item in events} == {EventRiskStatus.INSUFFICIENT_DATA}
    assert all(
        item.status is RiskCheckStatus.INSUFFICIENT_DATA
        for item in checks
        if item.check_name.endswith("risk_assessment")
    )


def test_disabled_phase7_dimensions_are_explicitly_not_applicable() -> None:
    checks, events = production_checks(
        policy=_policy(require_portfolio_metric_binding=False),
        snapshot=EvidenceSnapshot(),
        evaluated_at=MOMENT,
        account_binding=None,
    )
    assert next(item for item in checks if item.check_name == "portfolio_account_binding").status is RiskCheckStatus.NOT_APPLICABLE
    assert {item.status for item in events} == {EventRiskStatus.NOT_APPLICABLE}
