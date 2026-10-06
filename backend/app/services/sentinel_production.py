"""Provider-neutral Phase 7B/7C checks over immutable captured evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.evidence.models import EvidenceSnapshot, PortfolioRiskEvidence, PortfolioRiskMetricsEvidence
from app.models.portfolio_risk_metrics import PortfolioRiskMetricStatus, PortfolioRiskMetricType
from app.models.risk_assessment import (
    EventRiskStatus,
    RiskCheckInputReference,
    RiskCheckResult,
    RiskCheckStatus,
    SentinelAccountBinding,
    SentinelEventRiskAssessment,
)
from app.models.sentinel_policy import SentinelRiskPolicy


def production_checks(
    *,
    policy: SentinelRiskPolicy,
    snapshot: EvidenceSnapshot,
    evaluated_at: datetime,
    account_binding: SentinelAccountBinding | None,
) -> tuple[tuple[RiskCheckResult, ...], tuple[SentinelEventRiskAssessment, ...]]:
    """Return fail-closed checks; disabled policy dimensions are not applicable."""
    binding_check, portfolio, metrics = _account_binding_check(policy, snapshot, account_binding)
    checks = [binding_check]
    checks.extend(_freshness_checks(policy, snapshot, evaluated_at, portfolio, metrics))
    checks.extend(_metric_checks(policy, metrics, account_binding))
    events = _event_assessments(policy, snapshot, evaluated_at)
    checks.extend(_event_checks(policy, events))
    return tuple(checks), events


def _account_binding_check(
    policy: SentinelRiskPolicy,
    snapshot: EvidenceSnapshot,
    binding: SentinelAccountBinding | None,
) -> tuple[RiskCheckResult, PortfolioRiskEvidence | None, PortfolioRiskMetricsEvidence | None]:
    if not policy.require_portfolio_metric_binding:
        return _check("portfolio_account_binding", RiskCheckStatus.NOT_APPLICABLE), None, None
    if binding is None:
        return _check("portfolio_account_binding", RiskCheckStatus.INSUFFICIENT_DATA, "account_binding_missing"), None, None
    portfolios = [
        item for item in snapshot.portfolio_risk_snapshots
        if item.account == binding.account
        and item.snapshot_id == binding.portfolio_snapshot_id
        and item.fingerprint == binding.portfolio_snapshot_fingerprint
    ]
    if len(portfolios) != 1:
        return _check("portfolio_account_binding", RiskCheckStatus.INSUFFICIENT_DATA, "portfolio_snapshot_incompatible"), None, None
    portfolio = portfolios[0]
    metrics = [
        item for item in snapshot.portfolio_risk_metric_snapshots
        if item.account == binding.account
        and item.metrics_snapshot_id == binding.metrics_snapshot_id
        and item.fingerprint == binding.metrics_snapshot_fingerprint
        and item.source_snapshot_id == portfolio.snapshot_id
        and item.source_snapshot_fingerprint == portfolio.fingerprint
    ]
    if len(metrics) != 1:
        return _check("portfolio_account_binding", RiskCheckStatus.INSUFFICIENT_DATA, "metrics_snapshot_incompatible"), portfolio, None
    refs = (_ref("portfolio_snapshot", portfolio.snapshot_id, portfolio.fingerprint), _ref("portfolio_metrics", metrics[0].metrics_snapshot_id, metrics[0].fingerprint))
    return _check("portfolio_account_binding", RiskCheckStatus.PASS, references=refs), portfolio, metrics[0]


def _freshness_checks(policy: SentinelRiskPolicy, snapshot: EvidenceSnapshot, now: datetime, portfolio: PortfolioRiskEvidence | None, metrics: PortfolioRiskMetricsEvidence | None) -> tuple[RiskCheckResult, ...]:
    market_times = tuple(item.timestamp for item in snapshot.market_evidence)
    checks = [_freshness("market_freshness", market_times, policy.max_market_evidence_age_seconds, policy.rules.require_matching_market_evidence, now, policy.max_future_clock_skew_seconds)]
    checks.append(_freshness("portfolio_freshness", (() if portfolio is None else (portfolio.published_at or portfolio.as_of,)), policy.max_portfolio_evidence_age_seconds, policy.require_portfolio_metric_binding, now, policy.max_future_clock_skew_seconds))
    checks.append(_freshness("portfolio_metrics_freshness", (() if metrics is None else (metrics.as_of,)), policy.max_portfolio_metrics_age_seconds, policy.require_portfolio_metric_binding, now, policy.max_future_clock_skew_seconds))
    checks.append(_freshness("news_freshness", tuple(item.published_at for item in snapshot.news_evidence), policy.max_news_evidence_age_seconds, policy.require_news_event_assessment, now, policy.max_future_clock_skew_seconds))
    checks.append(_freshness("economic_event_freshness", tuple(item.released_at or item.scheduled_at for item in snapshot.economic_events), policy.max_economic_event_age_seconds, policy.require_economic_event_assessment, now, policy.max_future_clock_skew_seconds))
    checks.append(_freshness("corporate_action_freshness", tuple(item.effective_at or item.published_at for item in snapshot.corporate_actions), policy.max_corporate_action_age_seconds, policy.require_corporate_action_assessment, now, policy.max_future_clock_skew_seconds))
    return tuple(checks)


def _freshness(name: str, values: tuple[datetime, ...], maximum_age: int | None, required: bool, now: datetime, max_future_skew: int | None) -> RiskCheckResult:
    if not required or maximum_age is None:
        return _check(name, RiskCheckStatus.NOT_APPLICABLE)
    if not values:
        return _check(name, RiskCheckStatus.INSUFFICIENT_DATA, "required_evidence_missing")
    timestamp = max(values)
    if timestamp > now and (max_future_skew is None or (timestamp - now).total_seconds() > max_future_skew):
        return _check(name, RiskCheckStatus.INSUFFICIENT_DATA, "future_dated_evidence")
    if (now - timestamp).total_seconds() > maximum_age:
        return _check(name, RiskCheckStatus.INSUFFICIENT_DATA, "stale_evidence")
    return _check(name, RiskCheckStatus.PASS)


def _metric_checks(policy: SentinelRiskPolicy, metrics: PortfolioRiskMetricsEvidence | None, binding: SentinelAccountBinding | None) -> tuple[RiskCheckResult, ...]:
    definitions = (
        ("equity_utilization", PortfolioRiskMetricType.EQUITY_UTILIZATION, policy.max_equity_utilization),
        ("margin_utilization", PortfolioRiskMetricType.MARGIN_UTILIZATION, policy.max_margin_utilization),
        ("account_drawdown", PortfolioRiskMetricType.ACCOUNT_DRAWDOWN, policy.max_account_drawdown),
        ("daily_loss_utilization", PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION, policy.max_daily_loss_utilization),
        ("absolute_position_exposure", PortfolioRiskMetricType.POSITION_EXPOSURE, policy.max_absolute_position_exposure),
        ("portfolio_concentration", PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION, policy.max_portfolio_concentration),
    )
    result: list[RiskCheckResult] = []
    for name, kind, limit in definitions:
        if limit is None:
            result.append(_check(f"metric_{name}", RiskCheckStatus.NOT_APPLICABLE))
            continue
        if metrics is None:
            result.append(_check(f"metric_{name}", RiskCheckStatus.INSUFFICIENT_DATA, "metrics_snapshot_missing"))
            continue
        observed = [item.value for item in metrics.metrics if item.metric_type is kind and item.status is PortfolioRiskMetricStatus.AVAILABLE and item.value is not None]
        if not observed:
            result.append(_check(f"metric_{name}", RiskCheckStatus.INSUFFICIENT_DATA, "required_metric_unavailable"))
            continue
        if any(value > Decimal(str(limit)) for value in observed):
            result.append(_check(f"metric_{name}", RiskCheckStatus.REJECTED, "policy_metric_limit_exceeded"))
        else:
            result.append(_check(f"metric_{name}", RiskCheckStatus.PASS))
    return tuple(result)


def _event_assessments(policy: SentinelRiskPolicy, snapshot: EvidenceSnapshot, now: datetime) -> tuple[SentinelEventRiskAssessment, ...]:
    return (
        _event("news", policy.require_news_event_assessment, tuple(item.provenance for item in snapshot.news_evidence)),
        _event("economic_event", policy.require_economic_event_assessment, tuple(item.provenance for item in snapshot.economic_events)),
        _event("corporate_action", policy.require_corporate_action_assessment, tuple(item.provenance for item in snapshot.corporate_actions)),
    )


def _event(domain: str, required: bool, provenance: tuple[object, ...]) -> SentinelEventRiskAssessment:
    refs = tuple(_ref("evidence", f"{item.source_id}:{item.event_id}") for item in provenance)
    if not required:
        return SentinelEventRiskAssessment(domain=domain, status=EventRiskStatus.NOT_APPLICABLE, evidence_references=refs)
    if not refs:
        return SentinelEventRiskAssessment(domain=domain, status=EventRiskStatus.INSUFFICIENT_DATA, reason_codes=("required_event_evidence_missing",))
    return SentinelEventRiskAssessment(domain=domain, status=EventRiskStatus.ASSESSED_RELEVANT, evidence_references=refs, reason_codes=("immutable_event_evidence_present",))


def _event_checks(policy: SentinelRiskPolicy, assessments: tuple[SentinelEventRiskAssessment, ...]) -> tuple[RiskCheckResult, ...]:
    return tuple(
        _check(
            f"{item.domain}_risk_assessment",
            RiskCheckStatus.INSUFFICIENT_DATA if item.status is EventRiskStatus.INSUFFICIENT_DATA else (RiskCheckStatus.NOT_APPLICABLE if item.status is EventRiskStatus.NOT_APPLICABLE else RiskCheckStatus.PASS),
            *item.reason_codes,
            references=item.evidence_references,
        )
        for item in assessments
    )


def _ref(kind: str, identifier: str, fingerprint: str | None = None) -> RiskCheckInputReference:
    return RiskCheckInputReference(reference_type=kind, reference_id=identifier, fingerprint=fingerprint)


def _check(name: str, status: RiskCheckStatus, *codes: str, references: tuple[RiskCheckInputReference, ...] = ()) -> RiskCheckResult:
    return RiskCheckResult(check_name=name, status=status, reason_codes=tuple(codes), input_references=references)
