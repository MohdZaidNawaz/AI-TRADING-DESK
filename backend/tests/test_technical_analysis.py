"""Offline deterministic technical-analysis engine coverage."""

from datetime import UTC, datetime, timedelta

import pytest

from app.agents import create_friday_core
from app.evidence import MarketSeriesStore
from app.models.derived_features import FeatureStatus, FeatureType
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
    timeframe_seconds,
)
from app.models.schemas import AnalyzeRequest
from app.models.technical_analysis import (
    TechnicalCalculationStatus,
    TrendDirection,
)
from app.services.technical_analysis import TechnicalAnalysisService


START = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = MarketSeriesSource(
    source_id="offline-fixture",
    display_name="Offline fixture",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)


def _bar(
    index: int,
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
    timeframe: str = "15m",
    state: MarketBarState = MarketBarState.COMPLETED,
) -> NormalizedMarketBar:
    duration = timedelta(seconds=timeframe_seconds(timeframe))
    opened_at = START + duration * index
    return NormalizedMarketBar(
        source=SOURCE,
        symbol="XAUUSD",
        asset_class=AssetClass.COMMODITY,
        venue=None,
        timeframe=timeframe,
        opened_at=opened_at,
        closed_at=opened_at + duration,
        open=close,
        high=close + 1 if high is None else high,
        low=close - 1 if low is None else low,
        close=close,
        volume=100 + index,
        volume_kind=VolumeKind.TICK,
        state=state,
        received_at=opened_at + duration + timedelta(seconds=1),
        source_timestamp=opened_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _series(*bars: NormalizedMarketBar):
    store = MarketSeriesStore(max_series=20, max_bars_per_series=500)
    for bar in bars:
        store.ingest(bar)
    return store.snapshot(symbol="XAUUSD").series[0]


def test_complete_history_produces_real_trend_momentum_and_volatility() -> None:
    series = _series(*(_bar(index, close=100 + index) for index in range(250)))

    analysis = TechnicalAnalysisService().analyze((series,)).analyses[0]
    features = {feature.feature_type: feature for feature in analysis.indicators}

    assert analysis.trend_direction is TrendDirection.BULLISH
    assert features[FeatureType.RSI].value == pytest.approx(100.0)
    assert features[FeatureType.ATR].value == pytest.approx(2.0)
    assert features[FeatureType.ROC].value is not None
    assert features[FeatureType.ROLLING_STD].value is not None
    assert features[FeatureType.ROLLING_STD].value > 0
    assert all(feature.input_last_bar_id == series.bars[-1].bar_id for feature in analysis.indicators)
    assert analysis.provider_id == "offline-fixture"
    assert analysis.validated_candle_count == 250
    assert analysis.coverage_start == series.bars[0].opened_at
    assert analysis.coverage_end == series.bars[-1].closed_at


def test_insufficient_history_is_explicit_and_never_fabricates_values() -> None:
    analysis = TechnicalAnalysisService().analyze(
        (_series(*(_bar(index, close=100 + index) for index in range(5))),)
    ).analyses[0]

    assert analysis.status is TechnicalCalculationStatus.INSUFFICIENT_DATA
    assert analysis.trend_direction is TrendDirection.INSUFFICIENT_DATA
    assert all(feature.value is None for feature in analysis.indicators)
    assert all(feature.status is FeatureStatus.INSUFFICIENT_DATA for feature in analysis.indicators)
    assert "INDICATORS_INSUFFICIENT_OR_UNDEFINED" in analysis.warnings


def test_swing_structure_and_candidate_levels_follow_strict_extrema() -> None:
    highs = (3, 5, 4, 6, 4, 7, 5, 8, 6)
    lows = (1, 2, 1.5, 3, 2, 4, 3, 5, 4)
    bars = tuple(
        _bar(index, close=(high + low) / 2, high=high, low=low)
        for index, (high, low) in enumerate(zip(highs, lows, strict=True))
    )

    analysis = TechnicalAnalysisService(swing_window=1).analyze((_series(*bars),)).analyses[0]

    assert tuple(point.price for point in analysis.swing_highs) == (5.0, 6.0, 7.0, 8.0)
    assert tuple(point.price for point in analysis.swing_lows) == (1.5, 2.0, 3.0)
    assert "HIGHER_HIGH" in analysis.structure_observations
    assert "HIGHER_LOW" in analysis.structure_observations
    assert analysis.support_candidates[-1].price == 3.0
    assert analysis.resistance_candidates[-1].price == 8.0
    assert all(level.touches == 1 for level in analysis.support_candidates)


def test_provisional_candles_are_excluded_and_reported() -> None:
    bars = tuple(_bar(index, close=100 + index) for index in range(30)) + (
        _bar(30, close=1_000, state=MarketBarState.PROVISIONAL),
    )

    analysis = TechnicalAnalysisService().analyze((_series(*bars),)).analyses[0]

    assert analysis.provisional_candle_count == 1
    assert "PROVISIONAL_CANDLES_EXCLUDED" in analysis.warnings
    available = tuple(
        feature for feature in analysis.indicators if feature.status is FeatureStatus.AVAILABLE
    )
    assert available
    assert all(feature.input_last_bar_id == bars[-2].bar_id for feature in available)


def test_multi_timeframe_report_uses_only_available_validated_series() -> None:
    fifteen = _series(*(_bar(index, close=100 + index, timeframe="15m") for index in range(40)))
    hourly = _series(*(_bar(index, close=200 + index, timeframe="1h") for index in range(40)))

    report = TechnicalAnalysisService().analyze(
        (hourly, fifteen),
        requested_timeframes=("15m", "4h"),
    )

    assert set(report.available_timeframes) == {"15m", "1h"}
    assert report.unavailable_timeframes == ("4h",)
    assert {analysis.timeframe for analysis in report.analyses} == {"15m", "1h"}
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_pipeline_exposes_calculated_report_and_chartist_uses_it() -> None:
    core = create_friday_core()
    bars = tuple(_bar(index, close=100 + index) for index in range(250))
    for bar in bars:
        core.ingest_market_bar(bar)

    response = core.analyze(
        AnalyzeRequest(
            symbol="XAUUSD",
            query="Perform technical analysis on XAUUSD 15m",
        )
    )
    chartist = next(
        result for result in response.agent_results if result.agent_id == "chartist_technician"
    )

    assert response.technical_analysis is not None
    assert response.technical_analysis.calculation_mode == "deterministic"
    assert response.technical_analysis.analyses[0].trend_direction is TrendDirection.BULLISH
    assert chartist.status == "completed"
    assert "mock technical review plan" not in chartist.summary.lower()
    assert "calculated deterministic technical observations" in chartist.summary.lower()
    assert "Calculated observations:" in response.synthesis.final_response
    assert response.technical_analysis.production_authorization_available is False
    assert response.technical_analysis.submission_allowed is False
    assert response.technical_analysis.execution_authority is False
