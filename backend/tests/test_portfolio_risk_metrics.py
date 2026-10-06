"""Phase 5B-10 tests for deterministic portfolio-risk metric evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import EvidenceRouter, EvidenceStore, PortfolioRiskMetricsStore
from app.evidence.models import PortfolioRiskEvidence
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.portfolio_risk import (
    AccountStatus,
    NormalizedPortfolioRiskSnapshot,
    PortfolioRiskSource,
    PositionStatus,
    ReportedAccountState,
    ReportedPortfolioPosition,
    ReportedPositionSide,
    ReportedRiskLimit,
    RiskLimitKind,
    SourceAccountKey,
)
from app.models.portfolio_risk_metrics import (
    CALCULATION_VERSION,
    PortfolioRiskMetricStatus,
    PortfolioRiskMetricType,
)
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistOperationalState
from app.services.portfolio_risk_metrics import PortfolioRiskMetricsService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = PortfolioRiskSource(
    source_id="metrics-source",
    display_name="Metrics source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.ALTERNATIVE_DATA,
)


def _value(value: str, unit: EconomicValueUnit = EconomicValueUnit.CURRENCY) -> EconomicValue:
    return EconomicValue(value=Decimal(value), unit=unit, scale=EconomicValueScale.ONE)


def _snapshot(
    index: int = 0,
    *,
    occurrence: str | None = None,
    equity: str | None = "900",
    reference_equity: str | None = "1000",
    balance: str | None = "1000",
    margin_used: str | None = "200",
    margin_available: str | None = "800",
    daily_loss: str | None = "50",
    positions: tuple[ReportedPortfolioPosition, ...] | None = None,
) -> NormalizedPortfolioRiskSnapshot:
    account = SourceAccountKey(source_id=SOURCE.source_id, source_account_id="account-1")
    moment = MOMENT + timedelta(days=index)
    return NormalizedPortfolioRiskSnapshot(
        source=SOURCE,
        account=account,
        snapshot_occurrence_id=occurrence or f"occurrence-{index}",
        account_state=ReportedAccountState(
            currency="USD",
            balance=_value(balance) if balance is not None else None,
            equity=_value(equity) if equity is not None else None,
            reference_equity=_value(reference_equity) if reference_equity is not None else None,
            margin_used=_value(margin_used) if margin_used is not None else None,
            margin_available=_value(margin_available) if margin_available is not None else None,
            daily_loss=_value(daily_loss) if daily_loss is not None else None,
            status=AccountStatus.ACTIVE,
        ),
        positions=positions
        or (
            ReportedPortfolioPosition(
                source_position_id="position-a",
                source_instrument_id="instrument-a",
                source_symbol="XAUUSD",
                venue="synthetic",
                reported_side=ReportedPositionSide.LONG,
                quantity=_value("2", EconomicValueUnit.COUNT),
                price_currency="USD",
                reference_price=_value("100"),
                status=PositionStatus.OPEN,
            ),
            ReportedPortfolioPosition(
                source_position_id="position-b",
                source_instrument_id="instrument-b",
                source_symbol="EURUSD",
                venue="synthetic",
                reported_side=ReportedPositionSide.SHORT,
                quantity=_value("3", EconomicValueUnit.COUNT),
                price_currency="USD",
                reference_price=_value("100"),
                status=PositionStatus.OPEN,
            ),
        ),
        risk_limits=(
            ReportedRiskLimit(
                source_limit_id="daily-loss-limit",
                kind=RiskLimitKind.DAILY_LOSS,
                value=_value("100"),
            ),
        ),
        as_of=moment,
        published_at=moment,
        snapshot_version=1,
        received_at=moment + timedelta(minutes=1),
    )


def _derived(snapshot: NormalizedPortfolioRiskSnapshot):
    source = PortfolioRiskEvidence.from_snapshot(snapshot)
    return PortfolioRiskMetricsService().calculate(source), source


def _metric(snapshot, kind: PortfolioRiskMetricType, position: str | None = None):
    return next(
        item
        for item in snapshot.metrics
        if item.metric_type is kind and item.source_position_id == position
    )


def test_exact_decimal_metrics_and_explicit_dependencies() -> None:
    derived, _ = _derived(_snapshot())
    assert derived.calculation_version == CALCULATION_VERSION
    assert _metric(derived, PortfolioRiskMetricType.EQUITY_UTILIZATION).value == Decimal("0.9")
    assert _metric(derived, PortfolioRiskMetricType.MARGIN_UTILIZATION).value == Decimal("0.2")
    assert _metric(derived, PortfolioRiskMetricType.ACCOUNT_DRAWDOWN).value == Decimal("0.1")
    assert _metric(derived, PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION).value == Decimal("0.5")
    assert _metric(derived, PortfolioRiskMetricType.POSITION_EXPOSURE, "position-a").value == Decimal("200")
    assert _metric(derived, PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION, "position-a").value == Decimal("0.4")
    assert _metric(derived, PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION, "position-b").value == Decimal("0.6")
    assert all(metric.input_references for metric in derived.metrics)
    with pytest.raises(ValidationError):
        derived.metrics[0].value = Decimal("1")  # type: ignore[misc]


def test_missing_and_invalid_inputs_are_explicit_not_inferred() -> None:
    derived, _ = _derived(_snapshot(balance=None, reference_equity=None, margin_available="-1", daily_loss=None))
    assert _metric(derived, PortfolioRiskMetricType.EQUITY_UTILIZATION).status is PortfolioRiskMetricStatus.INSUFFICIENT_INPUTS
    assert _metric(derived, PortfolioRiskMetricType.ACCOUNT_DRAWDOWN).value is None
    assert _metric(derived, PortfolioRiskMetricType.MARGIN_UTILIZATION).value is not None
    assert _metric(derived, PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION).value is None
    zero_margin, _ = _derived(_snapshot(margin_used="0", margin_available="0"))
    assert _metric(zero_margin, PortfolioRiskMetricType.MARGIN_UTILIZATION).value is None
    missing_price_position = ReportedPortfolioPosition(
        source_position_id="missing-price", source_instrument_id="instrument", quantity=_value("1", EconomicValueUnit.COUNT)
    )
    incomplete, _ = _derived(_snapshot(positions=(missing_price_position,)))
    assert _metric(incomplete, PortfolioRiskMetricType.POSITION_EXPOSURE, "missing-price").value is None
    assert _metric(incomplete, PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION, "missing-price").value is None
    without_limit = _snapshot()
    without_limit = NormalizedPortfolioRiskSnapshot.model_validate(
        {**without_limit.model_dump(round_trip=True), "risk_limits": ()}
    )
    no_limit_metrics, _ = _derived(without_limit)
    assert _metric(no_limit_metrics, PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION).value is None
    duplicate_limit = _snapshot()
    duplicate_limit = NormalizedPortfolioRiskSnapshot.model_validate(
        {
            **duplicate_limit.model_dump(round_trip=True),
            "risk_limits": (
                *duplicate_limit.model_dump(round_trip=True)["risk_limits"],
                {"source_limit_id": "daily-loss-limit-2", "kind": "daily_loss", "value": _value("200").model_dump(mode="json")},
            ),
        }
    )
    ambiguous_metrics, _ = _derived(duplicate_limit)
    assert _metric(ambiguous_metrics, PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION).value is None


def test_identity_fingerprint_determinism_and_no_receipt_time_semantics() -> None:
    first, _ = _derived(_snapshot())
    retry = _snapshot()
    retry = NormalizedPortfolioRiskSnapshot.model_validate(
        {**retry.model_dump(round_trip=True), "received_at": MOMENT + timedelta(days=2)}
    )
    second, _ = _derived(retry)
    assert first.metrics_snapshot_id == second.metrics_snapshot_id
    assert first.fingerprint == second.fingerprint
    assert tuple(item.metric_id for item in first.metrics) == tuple(item.metric_id for item in second.metrics)


def test_store_duplicates_conflicts_eviction_boundedness_and_concurrency() -> None:
    first, source = _derived(_snapshot(0))
    store = PortfolioRiskMetricsStore(max_accounts=1, max_snapshots_per_account=1, max_snapshot_identities=2)
    assert store.ingest(first, source) is not None
    assert store.ingest(first, source) is not None
    second, second_source = _derived(_snapshot(1))
    assert store.ingest(second, second_source) is not None
    assert store.snapshot_count == 1
    # The retained full record may be gone, but the bounded fingerprint
    # registry still makes the exact calculation idempotent and conflict-safe.
    assert store.ingest(first, source) is None
    # Reordering is canonicalized, so a semantic conflict must alter an actual metric value.
    changed_metric = first.metrics[0].model_copy(update={"value": Decimal("99"), "status": PortfolioRiskMetricStatus.AVAILABLE})
    conflicting = first.model_copy(update={"metrics": (changed_metric, *first.metrics[1:])})
    with pytest.raises(ValueError, match="conflicting portfolio-risk metric"):
        store.ingest(conflicting, source)
    assert store.identity_count <= 2

    deterministic_one = PortfolioRiskMetricsStore(max_accounts=1, max_snapshots_per_account=5)
    deterministic_two = PortfolioRiskMetricsStore(max_accounts=1, max_snapshots_per_account=5)
    pairs = [_derived(_snapshot(index)) for index in range(4)]
    for item, evidence in pairs:
        deterministic_one.ingest(item, evidence)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tuple(pool.map(lambda pair: deterministic_two.ingest(*pair), reversed(pairs)))
    assert deterministic_one.snapshot() == deterministic_two.snapshot()


def test_source_account_and_position_scope_are_never_reconciled() -> None:
    first, _ = _derived(_snapshot())
    other_source = PortfolioRiskSource(
        source_id="other-metrics-source",
        display_name="Other Metrics Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.ALTERNATIVE_DATA,
    )
    other = _snapshot()
    other = NormalizedPortfolioRiskSnapshot.model_validate(
        {
            **other.model_dump(round_trip=True),
            "source": other_source.model_dump(mode="json"),
            "account": {"source_id": other_source.source_id, "source_account_id": "account-1"},
        }
    )
    second, _ = _derived(other)
    assert first.metrics_snapshot_id != second.metrics_snapshot_id
    assert {metric.account.source_id for metric in first.metrics} == {SOURCE.source_id}
    assert {metric.account.source_id for metric in second.metrics} == {other_source.source_id}


def test_routing_sentinel_artifacts_master_pilot_and_traceability() -> None:
    store = EvidenceStore()
    source = _snapshot()
    store.ingest_portfolio_risk_snapshot(source)
    evidence = store.snapshot(symbol="XAUUSD")
    router = EvidenceRouter()
    sentinel = router.route(agent_id="sentinel_risk_officer", snapshot=evidence)
    assert len(sentinel.portfolio_risk_metric_snapshots) == 1
    assert router.route(agent_id="ledger_the_book", snapshot=evidence).portfolio_risk_metric_snapshots == ()
    for agent_id in ("atlas_macro", "capitol_smart_money", "scout_recon", "athena_analyst", "chartist_technician", "oracle_quant", "pilot_execution"):
        assert router.route(agent_id=agent_id, snapshot=evidence).portfolio_risk_metric_snapshots == ()
    core = FridayCore(specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store)
    run = core._analyze_run(  # noqa: SLF001
        AnalyzeRequest(symbol="XAUUSD", query="risk portfolio review"), snapshot=evidence, lifecycle=__import__("app.agents.base", fromlist=["AnalysisLifecycle"]).AnalysisLifecycle()
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    assert artifacts["sentinel_risk_officer"].portfolio_risk_metric_observations
    assert artifacts["pilot_execution"].operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert artifacts["ledger_the_book"].portfolio_risk_metric_observations == ()
    assert any(item.event_id == evidence.portfolio_risk_metric_snapshots[0].metrics_snapshot_id for item in build_evidence_bundle(evidence).references)
    assert set(run.analysis.model_dump()) == {"analysis_id", "mode", "symbol", "query", "selected_agents", "routing", "agent_results", "synthesis"}
    assert not any(hasattr(metric, name) for metric in evidence.portfolio_risk_metric_snapshots[0].metrics for name in ("direction", "entry", "stop", "target", "sizing"))
