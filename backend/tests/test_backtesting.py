"""Offline coverage for Phase 5.7 deterministic historical validation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from pydantic import ValidationError

from app.api.routes import backtests
from app.evidence import MarketSeriesStore
from app.evidence.models import EvidenceSnapshot
from app.models.backtesting import (
    BacktestDirection,
    BacktestExitReason,
    BacktestParameters,
    BacktestRequest,
    BacktestStatus,
    CostVerificationStatus,
    MetricStatus,
    VerifiedTradingCosts,
)
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
)
from app.models.risk_strategy_intelligence import (
    AccountRiskInput,
    CalculationStatus,
    InstrumentRiskSpecification,
    PositionSizingResult,
    RiskSizingContext,
)
from app.services.backtesting import BacktestSignal, BacktestingService, get_backtesting_service
from app.services.research_requests import ResearchRequestService


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SOURCE = MarketSeriesSource(
    source_id="phase57-offline",
    display_name="Phase 5.7 Offline",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)


class _Owner:
    def __init__(self, series=()) -> None:
        self.series = tuple(series)
        self.calls = 0

    def evidence_snapshot(self, *, symbol=None):
        self.calls += 1
        return EvidenceSnapshot(
            market_series=tuple(
                item for item in self.series if symbol is None or item.symbol == symbol
            ),
            symbol=symbol,
        )


def _bars(
    values: list[tuple[float, float, float, float]],
    *,
    gap_after: int | None = None,
    provisional_last: bool = False,
):
    bars = []
    offset = 0
    for index, (open_, high, low, close) in enumerate(values):
        if gap_after is not None and index > gap_after:
            offset = 1
        opened = NOW + timedelta(minutes=15 * (index + offset))
        bars.append(
            NormalizedMarketBar(
                source=SOURCE,
                symbol="XAU/USD",
                asset_class=AssetClass.COMMODITY,
                venue=None,
                timeframe="15m",
                opened_at=opened,
                closed_at=opened + timedelta(minutes=15),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000 + index,
                volume_kind=VolumeKind.TICK,
                state=(
                    MarketBarState.PROVISIONAL
                    if provisional_last and index == len(values) - 1
                    else MarketBarState.COMPLETED
                ),
                received_at=opened + timedelta(minutes=15),
                source_timestamp=opened.isoformat(),
            )
        )
    return tuple(bars)


def _series(values, **kwargs):
    store = MarketSeriesStore(max_series=2, max_bars_per_series=500)
    for bar in _bars(values, **kwargs):
        store.ingest(bar)
    return store.snapshot(symbol="XAU/USD").series[0]


def _request(**updates):
    values = {
        "symbol": "XAUUSD",
        "timeframe": "15m",
        "parameters": BacktestParameters(development_fraction=0.5),
    }
    values.update(updates)
    return BacktestRequest(**values)


def _signal(series, *, direction=BacktestDirection.LONG, sizing=None):
    latest = series.latest_completed_bar
    assert latest is not None
    return BacktestSignal(
        direction=direction,
        signal_at=latest.closed_at,
        entry_lower=99.5,
        entry_upper=100.5,
        entry_reference=100.0,
        stop_loss=95.0 if direction is BacktestDirection.LONG else 105.0,
        take_profit=105.0 if direction is BacktestDirection.LONG else 95.0,
        evidence_ids=(latest.bar_id,),
        position_sizing=sizing,
    )


def _service(series, evaluator, *, minimum=1):
    return BacktestingService(
        evidence_owner=_Owner((series,)),
        resolver=ResearchRequestService(now=lambda: NOW),
        signal_evaluator=evaluator,
        minimum_history_bars=minimum,
        now=lambda: NOW + timedelta(days=1),
    )


def test_signal_uses_completed_prefix_and_entry_cannot_occur_on_signal_bar() -> None:
    series = _series([(100, 101, 99, 100), (100, 102, 99, 101), (101, 106, 100, 105)])
    seen: list[tuple[int, datetime]] = []

    def evaluator(prefix, _sizing, _evaluated):
        seen.append((prefix.completed_count, prefix.latest_completed_bar.closed_at))
        return _signal(prefix) if prefix.completed_count == 1 else None

    report = _service(series, evaluator).run_series(_request(), series)

    assert seen[0][0] == 1
    assert report.trades[0].signal_at == series.bars[0].closed_at
    assert report.trades[0].entered_at == series.bars[1].opened_at
    assert report.trades[0].entered_at >= report.trades[0].signal_at


def test_stop_and_target_collision_uses_conservative_stop_first() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 94, 100)])
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(_request(), series)

    assert report.trades[0].exit_reason is BacktestExitReason.STOP_LOSS
    assert report.trades[0].exit_price == 95.0
    assert report.trades[0].gross_return_pct == pytest.approx(-5.0)


def test_gap_through_stop_uses_observed_open_and_never_invents_execution_price() -> None:
    series = _series(
        [(100, 101, 99, 100), (100, 102, 99, 101), (90, 92, 88, 91)]
    )
    report = _service(
        series,
        lambda prefix, *_: _signal(prefix) if prefix.completed_count == 1 else None,
    ).run_series(_request(), series)

    assert report.trades[0].exit_reason is BacktestExitReason.GAP_STOP
    assert report.trades[0].exit_price == 90.0


def test_gap_beyond_stop_before_entry_is_skipped_as_unresolved_sequence() -> None:
    series = _series([(100, 101, 99, 100), (90, 101, 89, 100)])
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(_request(), series)

    assert report.trades == ()
    assert "ENTRY_SKIPPED_UNRESOLVED_GAP_SEQUENCE" in report.warnings


def test_repeated_signals_do_not_create_overlapping_trades() -> None:
    series = _series(
        [(100, 101, 99, 100), (100, 102, 99, 101), (101, 102, 99, 100), (100, 106, 99, 105)]
    )
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(_request(), series)

    assert len(report.trades) == 1
    assert report.parameters.allow_overlapping_positions is False


def test_provisional_and_missing_candles_are_reported_not_filled() -> None:
    series = _series(
        [(100, 101, 99, 100), (100, 102, 99, 101), (101, 102, 100, 101)],
        gap_after=0,
        provisional_last=True,
    )
    report = _service(series, lambda *_: None).run_series(_request(), series)

    assert report.data_quality.provisional_bar_count == 1
    assert report.data_quality.missing_interval_count == 1
    assert "PROVISIONAL_CANDLES_EXCLUDED" in report.warnings
    assert "MISSING_INTERVALS_PRESENT" in report.warnings


def test_duplicate_timestamp_rejects_simulation() -> None:
    series = _series([(100, 101, 99, 100), (100, 102, 99, 101)])
    duplicate = replace(series, bars=(series.bars[0], series.bars[0], series.bars[1]))

    report = _service(duplicate, lambda *_: None).run_series(_request(), duplicate)

    assert report.status is BacktestStatus.UNAVAILABLE
    assert report.data_quality.duplicate_timestamp_count == 1
    assert not report.data_quality.is_ordered


def _sizing_context():
    return RiskSizingContext(
        account=AccountRiskInput(
            equity=10_000,
            risk_percentage=1,
            currency="USD",
            observed_at=NOW,
            source_id="verified-test",
        ),
        instrument=InstrumentRiskSpecification(
            symbol="XAUUSD",
            asset_class=AssetClass.COMMODITY,
            size_unit="lots",
            minimum_size=0.01,
            maximum_size=10,
            size_step=0.01,
            tick_size=0.1,
            tick_value=1,
            tick_value_currency="USD",
            source_id="verified-test",
            verified_at=NOW,
        ),
    )


def test_verified_sizing_and_costs_produce_separate_gross_and_net_results() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    sizing = PositionSizingResult(
        status=CalculationStatus.AVAILABLE,
        account_currency="USD",
        risk_percentage=1,
        risk_amount=100,
        position_size=1,
        size_unit="lots",
        monetary_loss_per_size=50,
    )
    request = _request(
        sizing_context=_sizing_context(),
        verified_costs=VerifiedTradingCosts(
            spread_price=0.2,
            slippage_price_per_side=0.1,
            commission_per_size_round_trip=2,
            source_id="verified-test",
            verified_at=NOW,
        ),
    )
    report = _service(series, lambda prefix, *_: _signal(prefix, sizing=sizing)).run_series(request, series)

    trade = report.trades[0]
    assert trade.position_size == 1
    assert trade.gross_monetary_pnl == pytest.approx(50)
    assert trade.net_monetary_pnl is not None
    assert trade.net_monetary_pnl < trade.gross_monetary_pnl
    assert trade.net_return_pct is not None
    assert report.cost_basis == "verified_net"
    assert report.metrics.basis == "gross_returns"
    assert report.metrics.net is not None
    assert report.metrics.net.total_return.value < report.metrics.gross.total_return.value


def test_without_verified_costs_results_remain_explicitly_gross_only() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(_request(), series)

    assert report.trades[0].net_return_pct is None
    assert report.cost_basis == "gross_only"
    assert report.metrics.basis == "gross_returns"
    assert "VERIFIED_TRADING_COSTS_UNAVAILABLE_GROSS_RESULTS_ONLY" in report.warnings


def test_future_dated_verified_costs_are_not_applied() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    request = _request(
        verified_costs=VerifiedTradingCosts(
            spread_price=0.2,
            source_id="future-fixture",
            verified_at=NOW + timedelta(days=3),
        )
    )
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(request, series)

    assert report.trades[0].net_return_pct is None
    assert report.cost_basis == "gross_only"
    assert report.cost_assessment.status is CostVerificationStatus.INVALID
    assert "VERIFIED_COSTS_INVALID_AND_NOT_APPLIED" in report.warnings


def test_empty_and_partial_cost_evidence_never_implies_zero_costs() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    empty = _request(
        verified_costs=VerifiedTradingCosts(source_id="cost-source", verified_at=NOW)
    )
    partial = _request(
        verified_costs=VerifiedTradingCosts(
            spread_price=0,
            slippage_price_per_side=0,
            source_id="cost-source",
            verified_at=NOW,
        )
    )

    empty_report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(empty, series)
    partial_report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(partial, series)

    assert empty_report.cost_assessment.status is CostVerificationStatus.INCOMPLETE
    assert set(empty_report.cost_assessment.missing_components) == {
        "spread_price", "slippage_price_per_side", "commission_per_size_round_trip"
    }
    assert partial_report.cost_assessment.status is CostVerificationStatus.INCOMPLETE
    assert partial_report.cost_assessment.missing_components == ("commission_per_size_round_trip",)
    assert empty_report.metrics.net is None and partial_report.metrics.net is None
    assert all(item.net_return_pct is None for item in (*empty_report.trades, *partial_report.trades))


def test_explicit_verified_zero_costs_produce_net_metrics_equal_to_gross() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    request = _request(
        verified_costs=VerifiedTradingCosts(
            spread_price=0,
            slippage_price_per_side=0,
            commission_per_size_round_trip=0,
            source_id="verified-zero-costs",
            verified_at=NOW,
        )
    )
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(request, series)

    assert report.cost_assessment.status is CostVerificationStatus.VERIFIED
    assert report.cost_basis == "verified_net"
    assert report.metrics.net is not None
    assert report.metrics.net.total_return.value == pytest.approx(report.metrics.gross.total_return.value)
    assert report.trades[0].net_return_pct == pytest.approx(report.trades[0].gross_return_pct)


def test_gross_and_net_classification_are_independent_when_costs_reverse_a_win() -> None:
    series = _series([(100, 101, 99, 100), (100, 101.2, 99, 101)])
    request = _request(
        verified_costs=VerifiedTradingCosts(
            spread_price=2,
            slippage_price_per_side=0,
            commission_per_size_round_trip=0,
            source_id="verified-high-spread",
            verified_at=NOW,
        )
    )
    report = _service(
        series,
        lambda prefix, *_: replace(_signal(prefix), take_profit=101),
    ).run_series(request, series)

    assert report.metrics.gross.winning_trades == 1
    assert report.metrics.gross.losing_trades == 0
    assert report.metrics.net is not None
    assert report.metrics.net.winning_trades == 0
    assert report.metrics.net.losing_trades == 1
    assert report.metrics.gross.total_return.value > 0
    assert report.metrics.net.total_return.value < 0


def test_losing_trade_remains_a_loss_in_both_metric_families() -> None:
    series = _series([(100, 101, 99, 100), (100, 101, 94, 95)])
    request = _request(
        verified_costs=VerifiedTradingCosts(
            spread_price=0,
            slippage_price_per_side=0,
            commission_per_size_round_trip=0,
            source_id="verified-zero-costs",
            verified_at=NOW,
        )
    )
    report = _service(series, lambda prefix, *_: _signal(prefix)).run_series(request, series)

    assert report.metrics.gross.losing_trades == 1
    assert report.metrics.net is not None
    assert report.metrics.net.losing_trades == 1


def test_invalid_cost_metadata_is_rejected_by_the_typed_boundary() -> None:
    with pytest.raises(ValidationError):
        VerifiedTradingCosts(
            spread_price=-0.1,
            slippage_price_per_side=0,
            commission_per_size_round_trip=0,
            source_id="cost-source",
            verified_at=NOW,
        )
    with pytest.raises(ValidationError):
        VerifiedTradingCosts(
            spread_price=0,
            slippage_price_per_side=0,
            commission_per_size_round_trip=0,
            source_id="   ",
            verified_at=NOW,
        )


def test_zero_trade_metrics_and_sharpe_are_safely_undefined() -> None:
    series = _series([(100, 101, 99, 100), (100, 101, 99, 100)])
    report = _service(series, lambda *_: None).run_series(_request(), series)

    assert report.metrics.total_trades == 0
    assert report.metrics.win_rate.status is MetricStatus.UNDEFINED
    assert report.metrics.profit_factor.status is MetricStatus.UNDEFINED
    assert report.metrics.sharpe_ratio.status is MetricStatus.INSUFFICIENT_SAMPLE


def test_verified_zero_costs_keep_empty_gross_and_net_metrics_defined_as_unavailable() -> None:
    series = _series([(100, 101, 99, 100), (100, 101, 99, 100)])
    request = _request(
        verified_costs=VerifiedTradingCosts(
            spread_price=0,
            slippage_price_per_side=0,
            commission_per_size_round_trip=0,
            source_id="verified-zero-costs",
            verified_at=NOW,
        )
    )
    report = _service(series, lambda *_: None).run_series(request, series)

    assert report.metrics.gross.total_return.status is MetricStatus.UNDEFINED
    assert report.metrics.net is not None
    assert report.metrics.net.total_return.status is MetricStatus.UNDEFINED


def test_development_oos_and_walk_forward_windows_are_chronological() -> None:
    values = [(100, 101, 99, 100)] * 50
    series = _series(values)
    request = _request(
        parameters=BacktestParameters(
            development_fraction=0.5,
            walk_forward_window_bars=25,
            walk_forward_step_bars=25,
        )
    )
    report = _service(series, lambda *_: None).run_series(request, series)

    assert [item.kind.value for item in report.validation_windows[:2]] == [
        "development",
        "out_of_sample",
    ]
    assert report.validation_windows[0].end_at == report.validation_windows[1].start_at
    assert len(report.validation_windows) == 4
    assert all(
        left.start_at <= right.start_at
        for left, right in zip(report.validation_windows[2:], report.validation_windows[3:], strict=False)
    )


def test_validation_windows_resimulate_and_exclude_cross_boundary_positions() -> None:
    series = _series([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 102, 99, 101),
        (101, 106, 100, 105),
        (105, 106, 104, 105),
        (105, 106, 104, 105),
    ])

    def evaluator(prefix, *_):
        return _signal(prefix) if prefix.completed_count == 2 else None

    report = _service(series, evaluator).run_series(_request(), series)
    development, out_of_sample = report.validation_windows[:2]

    assert len(report.trades) == 1  # The full sample may complete the trade.
    assert development.metrics.total_trades == 0
    assert development.incomplete_trade_count == 1
    assert "OPEN_POSITION_EXCLUDED_AT_WINDOW_END" in development.warnings
    assert out_of_sample.metrics.total_trades == 0


def test_walk_forward_window_does_not_use_a_later_bar_to_close_a_position() -> None:
    series = _series([(100, 101, 99, 100)] * 24 + [(100, 102, 99, 101), (101, 106, 100, 105)] + [(105, 106, 104, 105)] * 24)
    request = _request(
        parameters=BacktestParameters(
            development_fraction=0.5,
            walk_forward_window_bars=25,
            walk_forward_step_bars=25,
        )
    )
    report = _service(
        series,
        lambda prefix, *_: _signal(prefix) if prefix.completed_count == 24 else None,
    ).run_series(request, series)
    first_walk = report.validation_windows[2]

    assert first_walk.metrics.total_trades == 0
    assert first_walk.incomplete_trade_count == 1
    assert "OPEN_POSITION_EXCLUDED_AT_WINDOW_END" in first_walk.warnings


def test_final_open_position_is_reported_but_not_forced_into_metrics() -> None:
    series = _series([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 102, 99, 101),
    ])
    report = _service(
        series,
        lambda prefix, *_: _signal(prefix) if prefix.completed_count == 2 else None,
    ).run_series(_request(), series)

    assert report.trades == ()
    assert report.incomplete_trade_count == 1
    assert report.metrics.total_trades == 0
    assert "OPEN_POSITION_EXCLUDED_AT_SAMPLE_END" in report.warnings


def test_last_bar_is_not_evaluated_as_a_signal_without_an_entry_bar() -> None:
    series = _series([(100, 101, 99, 100)] * 4)
    evaluated_prefixes: list[int] = []

    def evaluator(prefix, *_):
        evaluated_prefixes.append(prefix.completed_count)
        return None

    report = _service(series, evaluator).run_series(_request(), series)

    assert 4 not in evaluated_prefixes
    assert report.incomplete_trade_count == 0


def test_oos_uses_prior_bars_for_warmup_but_never_opens_a_pre_window_trade() -> None:
    series = _series([(100, 101, 99, 100)] * 8)
    seen: list[int] = []

    def evaluator(prefix, *_):
        seen.append(prefix.completed_count)
        return None

    report = _service(series, evaluator, minimum=3).run_series(_request(), series)
    out_of_sample = report.validation_windows[1]

    assert out_of_sample.observation_count == 4
    assert 4 in seen  # The first OOS bar can use its earlier completed history as warmup.
    assert out_of_sample.metrics.total_trades == 0


def test_post_development_bars_cannot_change_development_metrics() -> None:
    prefix = [
        (100, 101, 99, 100),
        (100, 106, 99, 105),
        (105, 106, 104, 105),
        (105, 106, 104, 105),
    ]
    first = _series(prefix + [(100, 110, 90, 105)] * 4)
    second = _series(prefix + [(100, 101, 99, 100)] * 4)
    request = _request()
    evaluator = lambda prefix, *_: _signal(prefix) if prefix.completed_count == 1 else None
    first_report = _service(first, evaluator).run_series(request, first)
    second_report = _service(second, evaluator).run_series(request, second)

    assert first_report.validation_windows[0].metrics.total_trades == 1
    assert first_report.validation_windows[0].metrics == second_report.validation_windows[0].metrics


def test_default_strategy_requires_its_200_candle_indicator_history() -> None:
    series = _series([(100, 101, 99, 100)] * 30)
    service = BacktestingService(
        evidence_owner=_Owner((series,)),
        resolver=ResearchRequestService(now=lambda: NOW),
        now=lambda: NOW + timedelta(days=1),
    )
    report = service.run_series(_request(), series)

    assert report.status is BacktestStatus.INSUFFICIENT_DATA
    assert "INSUFFICIENT_COMPLETED_CANDLES" in report.warnings


def test_default_strategy_adapter_runs_existing_technical_liquidity_and_risk_services() -> None:
    values = []
    for index in range(205):
        close = 100 + index * 0.08 + (index % 8) * 0.12
        values.append((close - 0.05, close + 0.35, close - 0.35, close))
    series = _series(values)
    service = BacktestingService(
        evidence_owner=_Owner((series,)),
        resolver=ResearchRequestService(now=lambda: NOW),
        now=lambda: NOW + timedelta(days=10),
    )

    report = service.run_series(_request(), series)

    assert report.strategy_id == "trend_confirmed_scenario_v1"
    assert report.status in {BacktestStatus.AVAILABLE, BacktestStatus.PARTIAL}
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_service_resolves_instrument_and_never_fetches_provider_data() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    owner = _Owner((series,))
    service = BacktestingService(
        evidence_owner=owner,
        resolver=ResearchRequestService(now=lambda: NOW),
        signal_evaluator=lambda prefix, *_: _signal(prefix),
        minimum_history_bars=1,
        now=lambda: NOW + timedelta(days=1),
    )
    response = service.run(_request())

    assert owner.calls == 1
    assert response.report.symbol == "XAU/USD"
    assert response.report.production_authorization_available is False
    assert "historical validation" in response.quantitative_specialist.summary.lower()
    assert "historical validation" in response.master_synthesis.final_response.lower()


def test_ambiguous_cached_sources_fail_closed_without_selecting_a_provider() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    other = replace(
        series,
        series_id="other-series",
        provenance=replace(series.provenance, source_id="other-source"),
    )
    service = BacktestingService(
        evidence_owner=_Owner((series, other)),
        resolver=ResearchRequestService(now=lambda: NOW),
        minimum_history_bars=1,
        now=lambda: NOW + timedelta(days=1),
    )
    response = service.run(_request())

    assert response.report.status is BacktestStatus.UNAVAILABLE
    assert "HISTORICAL_SOURCE_AMBIGUOUS" in response.report.warnings


def test_request_validation_rejects_invalid_ranges_and_partial_walk_forward() -> None:
    with pytest.raises(ValidationError):
        _request(start_at=NOW, end_at=NOW)
    with pytest.raises(ValidationError):
        BacktestParameters(walk_forward_window_bars=25)
    with pytest.raises(ValidationError):
        _request(timeframe="banana")


def test_api_uses_typed_service_dependency_and_rejects_extra_authority_input() -> None:
    series = _series([(100, 101, 99, 100), (100, 106, 99, 105)])
    service = _service(series, lambda prefix, *_: _signal(prefix))
    app = FastAPI()
    app.include_router(backtests.router, prefix="/api")
    app.dependency_overrides[get_backtesting_service] = lambda: service

    async def invoke(payload):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/backtests", json=payload)

    response = asyncio.run(invoke({"symbol": "XAUUSD", "timeframe": "15m"}))
    rejected = asyncio.run(
        invoke(
            {
                "symbol": "XAUUSD",
                "timeframe": "15m",
                "execution_authority": True,
            }
        )
    )

    assert response.status_code == 200
    assert response.json()["report"]["execution_authority"] is False
    assert rejected.status_code == 422
