"""Offline Phase 5.5 deterministic liquidity-intelligence coverage."""

from datetime import UTC, datetime, timedelta

from app.agents import create_friday_core
from app.evidence import MarketSeriesStore
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.liquidity_intelligence import (
    FairValueGapState,
    LiquidityAnalysisStatus,
    LiquidityLevelState,
    OrderBlockState,
    PatternConfirmation,
    StructureBreakKind,
    StructureLabel,
    StructureTrend,
    TimeframeAlignmentState,
)
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
    timeframe_seconds,
)
from app.models.schemas import AnalyzeRequest
from app.services.liquidity_intelligence import LiquidityIntelligenceService


START = datetime(2026, 11, 1, tzinfo=UTC)
SOURCE = MarketSeriesSource(
    source_id="phase55-offline",
    display_name="Phase 5.5 Offline",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)


def _bar(
    index: int,
    *,
    high: float,
    low: float,
    open_price: float | None = None,
    close: float | None = None,
    timeframe: str = "15m",
    state: MarketBarState = MarketBarState.COMPLETED,
    received_at: datetime | None = None,
) -> NormalizedMarketBar:
    duration = timedelta(seconds=timeframe_seconds(timeframe))
    opened_at = START + duration * index
    resolved_open = (high + low) / 2 if open_price is None else open_price
    resolved_close = resolved_open if close is None else close
    return NormalizedMarketBar(
        source=SOURCE,
        symbol="XAUUSD",
        asset_class=AssetClass.COMMODITY,
        venue=None,
        timeframe=timeframe,
        opened_at=opened_at,
        closed_at=opened_at + duration,
        open=resolved_open,
        high=high,
        low=low,
        close=resolved_close,
        volume=100 + index,
        volume_kind=VolumeKind.TICK,
        state=state,
        received_at=received_at or opened_at + duration + timedelta(seconds=1),
        source_timestamp=opened_at.isoformat(),
    )


def _series(*bars: NormalizedMarketBar):
    store = MarketSeriesStore(max_series=20, max_bars_per_series=500)
    for bar in bars:
        store.ingest(bar)
    return store.snapshot(symbol="XAUUSD").series[0]


def _evaluation(*bars: NormalizedMarketBar) -> datetime:
    return max(item.received_at for item in bars) + timedelta(seconds=1)


def _trending_bars(*, timeframe: str = "15m") -> tuple[NormalizedMarketBar, ...]:
    highs = (3, 5, 4, 6, 4.5, 7, 5.5, 8, 6.5)
    lows = (1, 2, 1.5, 3, 2, 4, 3, 5, 4)
    return tuple(
        _bar(index, high=high, low=low, timeframe=timeframe)
        for index, (high, low) in enumerate(zip(highs, lows, strict=True))
    )


def test_confirmed_structure_labels_and_scope_reuse_symmetric_swings() -> None:
    bars = _trending_bars()
    analysis = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*bars),), evaluated_at=_evaluation(*bars)
    ).analyses[0]

    labels = {item.label for item in analysis.structure_points}
    assert StructureLabel.HIGHER_HIGH in labels
    assert StructureLabel.HIGHER_LOW in labels
    assert analysis.structure_trend is StructureTrend.BULLISH
    assert all(item.confirmation is PatternConfirmation.CONFIRMED for item in analysis.structure_points)
    assert all(
        max(reference.closed_at for reference in item.evidence) <= analysis.evaluated_at
        for item in analysis.structure_points
    )


def test_equal_high_cluster_and_rejection_create_price_action_sweep() -> None:
    highs = (9.0, 10.0, 9.0, 10.003, 9.0, 10.2, 9.0)
    bars = tuple(
        _bar(
            index,
            high=high,
            low=8.0,
            open_price=9.0,
            close=(9.9 if index == 5 else 9.0),
        )
        for index, high in enumerate(highs)
    )
    analysis = LiquidityIntelligenceService(
        swing_window=1,
        equal_level_tolerance_bps=5,
    ).analyze((_series(*bars),), evaluated_at=_evaluation(*bars)).analyses[0]

    buy_side = [item for item in analysis.liquidity_levels if item.side.value == "buy_side"]
    clustered = next(item for item in buy_side if item.touches == 2)
    sweep = next(item for item in analysis.sweeps if item.level_id == clustered.level_id)
    assert clustered.state is LiquidityLevelState.SWEPT
    assert sweep.confirmation is PatternConfirmation.CONFIRMED
    assert sweep.invalidated is False
    assert sweep.penetration_price == 10.2


def test_three_candle_fvg_fill_states_are_deterministic() -> None:
    formation = (
        _bar(0, high=101, low=99),
        _bar(1, high=102, low=100),
        _bar(2, high=105, low=103),
    )
    partial = _bar(3, high=104, low=102)
    full = _bar(4, high=103, low=100)
    service = LiquidityIntelligenceService(swing_window=1)

    untouched_report = service.analyze(
        (_series(*formation),), evaluated_at=_evaluation(*formation)
    ).analyses[0]
    partial_report = service.analyze(
        (_series(*formation, partial),), evaluated_at=_evaluation(*formation, partial)
    ).analyses[0]
    full_report = service.analyze(
        (_series(*formation, partial, full),),
        evaluated_at=_evaluation(*formation, partial, full),
    ).analyses[0]

    assert untouched_report.fair_value_gaps[0].state is FairValueGapState.UNTOUCHED
    assert partial_report.fair_value_gaps[0].state is FairValueGapState.PARTIALLY_FILLED
    assert full_report.fair_value_gaps[0].state is FairValueGapState.FULLY_FILLED
    assert full_report.fair_value_gaps[0].invalidated is True


def test_close_and_wick_breaks_are_distinct_and_order_blocks_require_close() -> None:
    bars = _trending_bars() + (
        _bar(9, high=8.5, low=5.5, open_price=7.0, close=7.5),
        _bar(10, high=8.2, low=5.8, open_price=7.5, close=6.5),
        _bar(11, high=9.0, low=6.2, open_price=6.5, close=8.6),
    )
    analysis = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*bars),), evaluated_at=_evaluation(*bars)
    ).analyses[0]

    assert any(item.confirmation is PatternConfirmation.WICK_ONLY for item in analysis.structure_breaks)
    confirmed = [
        item for item in analysis.structure_breaks if item.confirmation is PatternConfirmation.CONFIRMED
    ]
    assert confirmed
    assert analysis.order_block_candidates
    assert all(
        item.associated_break_id in {event.break_id for event in confirmed}
        for item in analysis.order_block_candidates
    )


def test_bearish_structure_then_close_break_is_bullish_choch() -> None:
    highs = (10.0, 8.0, 9.0, 7.0, 8.0, 6.0, 7.0, 9.0)
    lows = (8.0, 6.0, 7.0, 5.0, 6.0, 4.0, 5.0, 6.0)
    bars = tuple(
        _bar(
            index,
            high=high,
            low=low,
            open_price=(high + low) / 2,
            close=(8.5 if index == 7 else (high + low) / 2),
        )
        for index, (high, low) in enumerate(zip(highs, lows, strict=True))
    )
    analysis = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*bars),), evaluated_at=_evaluation(*bars)
    ).analyses[0]

    assert analysis.structure_trend is StructureTrend.BEARISH
    choch = next(
        item
        for item in analysis.structure_breaks
        if item.kind is StructureBreakKind.BULLISH_CHOCH
    )
    assert choch.confirmation is PatternConfirmation.CONFIRMED
    assert choch.reference_level == 8.0
    assert choch.break_price == 8.5


def test_order_block_mitigation_and_invalidation_are_separate_states() -> None:
    base = _trending_bars() + (
        _bar(9, high=8.5, low=5.5, open_price=7.0, close=7.5),
        _bar(10, high=8.2, low=5.8, open_price=7.5, close=6.5),
        _bar(11, high=9.0, low=6.2, open_price=6.5, close=8.6),
    )
    mitigated_bar = _bar(12, high=8.0, low=7.0, open_price=7.6, close=7.8)
    invalidating_bar = _bar(13, high=7.0, low=5.0, open_price=6.5, close=5.5)
    service = LiquidityIntelligenceService(swing_window=1)

    initial = service.analyze(
        (_series(*base),), evaluated_at=_evaluation(*base)
    ).analyses[0]
    mitigated = service.analyze(
        (_series(*base, mitigated_bar),),
        evaluated_at=_evaluation(*base, mitigated_bar),
    ).analyses[0]
    invalidated = service.analyze(
        (_series(*base, mitigated_bar, invalidating_bar),),
        evaluated_at=_evaluation(*base, mitigated_bar, invalidating_bar),
    ).analyses[0]

    assert initial.order_block_candidates[-1].state is OrderBlockState.UNMITIGATED
    assert mitigated.order_block_candidates[-1].state is OrderBlockState.MITIGATED
    assert invalidated.order_block_candidates[-1].state is OrderBlockState.INVALIDATED


def test_provisional_and_future_bars_never_confirm_or_repaint_history() -> None:
    base = _trending_bars()
    future = _bar(
        9,
        high=50,
        low=0.5,
        state=MarketBarState.COMPLETED,
        received_at=_evaluation(*base) + timedelta(hours=1),
    )
    provisional = _bar(10, high=100, low=0.1, state=MarketBarState.PROVISIONAL)
    evaluated_at = _evaluation(*base)
    analysis = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*base, future, provisional),), evaluated_at=evaluated_at
    ).analyses[0]

    assert analysis.excluded_future_candle_count == 1
    assert analysis.provisional_candle_count == 1
    assert analysis.coverage_end == base[-1].closed_at
    assert all(
        reference.bar_id not in {future.bar_id, provisional.bar_id}
        for item in analysis.structure_points
        for reference in item.evidence
    )
    assert "FUTURE_OR_LATE_RECEIVED_CANDLES_EXCLUDED" in analysis.warnings
    assert "PROVISIONAL_CANDLES_EXCLUDED" in analysis.warnings


def test_stale_missing_and_multi_timeframe_states_are_explicit() -> None:
    fifteen = _trending_bars(timeframe="15m")
    hourly = _trending_bars(timeframe="1h")
    current = max(_evaluation(*fifteen), _evaluation(*hourly))
    report = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*hourly), _series(*fifteen)),
        requested_timeframes=("15m", "1h", "4h"),
        evaluated_at=current,
    )

    assert report.alignment.state is TimeframeAlignmentState.ALIGNED_BULLISH
    assert report.unavailable_timeframes == ("4h",)
    stale = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*fifteen),),
        evaluated_at=current + timedelta(days=3),
    ).analyses[0]
    assert stale.status is LiquidityAnalysisStatus.STALE
    assert stale.freshness == "stale"
    assert "MARKET_SERIES_STALE" in stale.warnings


def test_missing_interval_limits_analysis_to_latest_contiguous_tail() -> None:
    early = tuple(_bar(index, high=10 + index, low=8 + index) for index in range(3))
    tail = tuple(
        _bar(index, high=20 + (index % 2), low=18 - (index % 2))
        for index in range(5, 10)
    )
    bars = (*early, *tail)
    analysis = LiquidityIntelligenceService(swing_window=1).analyze(
        (_series(*bars),), evaluated_at=_evaluation(*bars)
    ).analyses[0]

    assert analysis.missing_interval_count == 2
    assert analysis.completed_candle_count == len(tail)
    assert analysis.coverage_start == tail[0].opened_at
    assert "MISSING_INTERVALS_PRESENT" in analysis.warnings


def test_capitol_master_and_api_receive_real_report_without_execution_authority() -> None:
    core = create_friday_core()
    bars = _trending_bars()
    for bar in bars:
        core.ingest_market_bar(bar)

    response = core.analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Analyse liquidity, FVG, BOS and order blocks on 15m")
    )
    capitol = next(item for item in response.agent_results if item.agent_id == "capitol_smart_money")

    assert response.liquidity_intelligence is not None
    assert response.liquidity_intelligence.analyses
    assert "mock institutional-participation framework" not in capitol.summary
    assert "deterministic price-action liquidity intelligence" in capitol.summary
    assert "not observed institutional orders" in response.synthesis.final_response
    assert response.liquidity_intelligence.institutional_order_flow_observed is False
    assert response.liquidity_intelligence.production_authorization_available is False
    assert response.liquidity_intelligence.submission_allowed is False
    assert response.liquidity_intelligence.execution_authority is False


def test_result_order_and_fingerprints_are_deterministic_for_same_evidence() -> None:
    bars = _trending_bars()
    service = LiquidityIntelligenceService(swing_window=1)
    evaluation = _evaluation(*bars)

    first = service.analyze((_series(*bars),), evaluated_at=evaluation)
    second = service.analyze((_series(*bars),), evaluated_at=evaluation)

    assert first == second
