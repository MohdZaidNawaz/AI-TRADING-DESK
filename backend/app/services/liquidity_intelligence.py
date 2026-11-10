"""Deterministic price-action liquidity analysis over validated OHLCV evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from app.evidence.models import MarketBarEvidence, MarketSeriesEvidence
from app.models.liquidity_intelligence import (
    FairValueGap,
    FairValueGapDirection,
    FairValueGapState,
    LiquidityAnalysisStatus,
    LiquidityBarReference,
    LiquidityIntelligenceReport,
    LiquidityLevel,
    LiquidityLevelState,
    LiquiditySide,
    LiquiditySweep,
    LiquidityTimeframeAnalysis,
    OrderBlockCandidate,
    OrderBlockDirection,
    OrderBlockState,
    PatternConfirmation,
    StructureBreak,
    StructureBreakKind,
    StructureLabel,
    StructurePoint,
    StructureScope,
    StructureTrend,
    TimeframeAlignment,
    TimeframeAlignmentState,
)
from app.models.market_series import MarketBarState, timeframe_seconds
from app.models.technical_analysis import SwingKind
from app.services.technical_analysis import confirmed_swing_points


SUPPORTED_LIQUIDITY_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")


class LiquidityIntelligenceService:
    """Calculate bounded price-action candidates without claiming observed orders."""

    def __init__(
        self,
        *,
        swing_window: int = 2,
        equal_level_tolerance_bps: float = 5.0,
        minimum_gap_bps: float = 1.0,
        maximum_patterns_per_kind: int = 100,
    ) -> None:
        if swing_window < 1 or swing_window > 20:
            raise ValueError("swing_window must be between 1 and 20")
        if not 0 < equal_level_tolerance_bps <= 100:
            raise ValueError("equal-level tolerance must be between 0 and 100 bps")
        if not 0 <= minimum_gap_bps <= 100:
            raise ValueError("minimum-gap tolerance must be between 0 and 100 bps")
        if maximum_patterns_per_kind < 1 or maximum_patterns_per_kind > 500:
            raise ValueError("maximum_patterns_per_kind must be between 1 and 500")
        self._swing_window = swing_window
        self._equal_level_tolerance_bps = equal_level_tolerance_bps
        self._minimum_gap_bps = minimum_gap_bps
        self._maximum_patterns = maximum_patterns_per_kind

    def analyze(
        self,
        series: tuple[MarketSeriesEvidence, ...],
        *,
        requested_timeframes: tuple[str, ...] = (),
        evaluated_at: datetime | None = None,
    ) -> LiquidityIntelligenceReport:
        if any(not isinstance(item, MarketSeriesEvidence) for item in series):
            raise TypeError("series entries must be MarketSeriesEvidence")
        evaluation = _utc(evaluated_at or datetime.now(UTC))
        supported = tuple(
            item for item in series if item.timeframe in SUPPORTED_LIQUIDITY_TIMEFRAMES
        )
        analyses = tuple(
            self._analyze_timeframe(item, evaluated_at=evaluation)
            for item in sorted(
                supported,
                key=lambda item: (
                    timeframe_seconds(item.timeframe),
                    item.key.source_id,
                    item.series_id,
                ),
            )
        )
        available = tuple(dict.fromkeys(item.timeframe for item in analyses))
        unavailable = tuple(
            dict.fromkeys(
                timeframe
                for timeframe in requested_timeframes
                if timeframe and timeframe not in available
            )
        )
        return LiquidityIntelligenceReport(
            evaluated_at=evaluation,
            analyses=analyses,
            alignment=_alignment(analyses, unavailable),
            available_timeframes=available,
            unavailable_timeframes=unavailable,
        )

    def _analyze_timeframe(
        self,
        series: MarketSeriesEvidence,
        *,
        evaluated_at: datetime,
    ) -> LiquidityTimeframeAnalysis:
        eligible = tuple(
            bar
            for bar in series.bars
            if bar.state is MarketBarState.COMPLETED
            and bar.closed_at <= evaluated_at
            and bar.received_at <= evaluated_at
        )
        excluded_future = sum(
            bar.state is MarketBarState.COMPLETED
            and (bar.closed_at > evaluated_at or bar.received_at > evaluated_at)
            for bar in series.bars
        )
        contiguous = _contiguous_tail(eligible)
        swing_highs, swing_lows = confirmed_swing_points(
            contiguous,
            window=self._swing_window,
        )
        points = _structure_points(
            contiguous,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            window=self._swing_window,
            tolerance_bps=self._equal_level_tolerance_bps,
        )
        provisional_points = _provisional_structure_points(
            contiguous,
            window=self._swing_window,
        )
        trend = _structure_trend(points)
        levels, sweeps = _liquidity_levels_and_sweeps(
            contiguous,
            points,
            tolerance_bps=self._equal_level_tolerance_bps,
            limit=self._maximum_patterns,
        )
        gaps = _fair_value_gaps(
            contiguous,
            minimum_gap_bps=self._minimum_gap_bps,
            limit=self._maximum_patterns,
        )
        breaks = _structure_breaks(
            contiguous,
            points,
            limit=self._maximum_patterns,
        )
        blocks = _order_blocks(
            contiguous,
            breaks,
            limit=self._maximum_patterns,
        )
        stale_after = max(86_400, timeframe_seconds(series.timeframe) * 2)
        age_seconds = max(
            0.0,
            (evaluated_at - series.provenance.latest_received_at).total_seconds(),
        )
        freshness = "stale" if age_seconds > stale_after else "fresh"
        warnings: list[str] = []
        limitations: list[str] = [
            "Liquidity levels and order blocks are price-action candidates; resting institutional orders were not observed."
        ]
        if series.provisional_count:
            warnings.append("PROVISIONAL_CANDLES_EXCLUDED")
            limitations.append(
                f"Excluded {series.provisional_count} provisional candle(s) from confirmed patterns."
            )
        if excluded_future:
            warnings.append("FUTURE_OR_LATE_RECEIVED_CANDLES_EXCLUDED")
            limitations.append(
                f"Excluded {excluded_future} completed candle(s) unavailable at the evaluation timestamp."
            )
        if series.continuity.missing_interval_count:
            warnings.append("MISSING_INTERVALS_PRESENT")
            limitations.append("Only the latest contiguous completed-candle segment was evaluated.")
        if freshness == "stale":
            warnings.append("MARKET_SERIES_STALE")
            limitations.append("Patterns describe stale historical evidence and are not current market state.")
        minimum_bars = self._swing_window * 2 + 1
        if len(contiguous) < minimum_bars:
            warnings.append("INSUFFICIENT_COMPLETED_CANDLES")
            limitations.append(
                f"At least {minimum_bars} contiguous completed candles are required for confirmed pivots."
            )
            status = LiquidityAnalysisStatus.INSUFFICIENT_DATA
        elif freshness == "stale":
            status = LiquidityAnalysisStatus.STALE
        elif warnings:
            status = LiquidityAnalysisStatus.PARTIAL
        else:
            status = LiquidityAnalysisStatus.AVAILABLE
        return LiquidityTimeframeAnalysis(
            status=status,
            symbol=series.symbol,
            asset_class=series.asset_class,
            venue=series.venue,
            timeframe=series.timeframe,
            provider_id=series.provenance.source_id,
            provider_name=series.provenance.source_name,
            evaluated_at=evaluated_at,
            retrieved_at=series.provenance.latest_received_at,
            coverage_start=contiguous[0].opened_at if contiguous else None,
            coverage_end=contiguous[-1].closed_at if contiguous else None,
            freshness=freshness,
            completed_candle_count=len(contiguous),
            provisional_candle_count=series.provisional_count,
            excluded_future_candle_count=excluded_future,
            missing_interval_count=series.continuity.missing_interval_count,
            structure_trend=trend,
            structure_points=points[-self._maximum_patterns :],
            provisional_structure_points=provisional_points[-self._maximum_patterns :],
            liquidity_levels=levels[-self._maximum_patterns :],
            sweeps=sweeps[-self._maximum_patterns :],
            fair_value_gaps=gaps[-self._maximum_patterns :],
            structure_breaks=breaks[-self._maximum_patterns :],
            order_block_candidates=blocks[-self._maximum_patterns :],
            warnings=tuple(warnings),
            limitations=tuple(limitations),
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation timestamps must include a timezone")
    return value.astimezone(UTC)


def _reference(bar: MarketBarEvidence) -> LiquidityBarReference:
    return LiquidityBarReference(
        bar_id=bar.bar_id,
        opened_at=bar.opened_at,
        closed_at=bar.closed_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        state=bar.state,
    )


def _identifier(prefix: str, *parts: object) -> str:
    payload = "\n".join(str(part) for part in parts)
    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _contiguous_tail(bars: tuple[MarketBarEvidence, ...]) -> tuple[MarketBarEvidence, ...]:
    start = 0
    for index, (previous, current) in enumerate(zip(bars, bars[1:], strict=False), start=1):
        if previous.closed_at != current.opened_at:
            start = index
    return bars[start:]


def _tolerance(price: float, bps: float) -> float:
    return max(abs(price) * bps / 10_000.0, 1e-12)


def _structure_points(
    bars: tuple[MarketBarEvidence, ...],
    *,
    swing_highs: tuple,
    swing_lows: tuple,
    window: int,
    tolerance_bps: float,
) -> tuple[StructurePoint, ...]:
    bar_index = {bar.bar_id: index for index, bar in enumerate(bars)}
    previous: dict[str, list[float]] = {"high": [], "low": []}
    points: list[StructurePoint] = []
    swings = sorted((*swing_highs, *swing_lows), key=lambda item: (item.occurred_at, item.kind.value))
    for swing in swings:
        kind = "high" if swing.kind is SwingKind.HIGH else "low"
        prior = previous[kind]
        if not prior:
            label = StructureLabel.UNCLASSIFIED
            scope = StructureScope.EXTERNAL
        else:
            tolerance = _tolerance(prior[-1], tolerance_bps)
            if abs(swing.price - prior[-1]) <= tolerance:
                label = StructureLabel.EQUAL_HIGH if kind == "high" else StructureLabel.EQUAL_LOW
            elif kind == "high":
                label = StructureLabel.HIGHER_HIGH if swing.price > prior[-1] else StructureLabel.LOWER_HIGH
            else:
                label = StructureLabel.HIGHER_LOW if swing.price > prior[-1] else StructureLabel.LOWER_LOW
            scope = (
                StructureScope.EXTERNAL
                if (kind == "high" and swing.price > max(prior))
                or (kind == "low" and swing.price < min(prior))
                else StructureScope.INTERNAL
            )
        index = bar_index[swing.bar_id]
        evidence = tuple(
            _reference(bar)
            for bar in bars[max(0, index - window) : index + window + 1]
        )
        points.append(
            StructurePoint(
                swing_kind=kind,
                label=label,
                scope=scope,
                price=swing.price,
                occurred_at=swing.occurred_at,
                confirmation=PatternConfirmation.CONFIRMED,
                evidence=evidence,
            )
        )
        prior.append(swing.price)
    return tuple(points)


def _provisional_structure_points(
    bars: tuple[MarketBarEvidence, ...],
    *,
    window: int,
) -> tuple[StructurePoint, ...]:
    candidates: list[StructurePoint] = []
    for index in range(max(window, len(bars) - window), len(bars)):
        current = bars[index]
        left = bars[max(0, index - window) : index]
        if len(left) < window:
            continue
        evidence = tuple(_reference(bar) for bar in (*left, current))
        if all(current.high > bar.high for bar in left):
            candidates.append(
                StructurePoint(
                    swing_kind="high",
                    label=StructureLabel.UNCLASSIFIED,
                    scope=StructureScope.INTERNAL,
                    price=current.high,
                    occurred_at=current.opened_at,
                    confirmation=PatternConfirmation.PROVISIONAL,
                    evidence=evidence,
                )
            )
        if all(current.low < bar.low for bar in left):
            candidates.append(
                StructurePoint(
                    swing_kind="low",
                    label=StructureLabel.UNCLASSIFIED,
                    scope=StructureScope.INTERNAL,
                    price=current.low,
                    occurred_at=current.opened_at,
                    confirmation=PatternConfirmation.PROVISIONAL,
                    evidence=evidence,
                )
            )
    return tuple(candidates)


def _structure_trend(points: tuple[StructurePoint, ...]) -> StructureTrend:
    highs = [item for item in points if item.swing_kind == "high"]
    lows = [item for item in points if item.swing_kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return StructureTrend.INSUFFICIENT_DATA
    high_label = highs[-1].label
    low_label = lows[-1].label
    if high_label is StructureLabel.HIGHER_HIGH and low_label is StructureLabel.HIGHER_LOW:
        return StructureTrend.BULLISH
    if high_label is StructureLabel.LOWER_HIGH and low_label is StructureLabel.LOWER_LOW:
        return StructureTrend.BEARISH
    return StructureTrend.RANGE


def _liquidity_levels_and_sweeps(
    bars: tuple[MarketBarEvidence, ...],
    points: tuple[StructurePoint, ...],
    *,
    tolerance_bps: float,
    limit: int,
) -> tuple[tuple[LiquidityLevel, ...], tuple[LiquiditySweep, ...]]:
    levels: list[LiquidityLevel] = []
    sweeps: list[LiquiditySweep] = []
    for side, selected in (
        (LiquiditySide.BUY_SIDE, [item for item in points if item.swing_kind == "high"]),
        (LiquiditySide.SELL_SIDE, [item for item in points if item.swing_kind == "low"]),
    ):
        clusters: list[list[StructurePoint]] = []
        for point in selected:
            cluster = next(
                (
                    item
                    for item in clusters
                    if abs(point.price - sum(value.price for value in item) / len(item))
                    <= _tolerance(sum(value.price for value in item) / len(item), tolerance_bps)
                ),
                None,
            )
            if cluster is None:
                clusters.append([point])
            else:
                cluster.append(point)
        for cluster in clusters[-limit:]:
            price = sum(item.price for item in cluster) / len(cluster)
            tolerance = _tolerance(price, tolerance_bps)
            formed_at = max(item.occurred_at for item in cluster)
            evidence = tuple(
                {ref.bar_id: ref for item in cluster for ref in item.evidence}.values()
            )[-49:]
            level_id = _identifier("liquidity-level", side.value, price, formed_at.isoformat())
            after = tuple(bar for bar in bars if bar.opened_at > formed_at)
            state = LiquidityLevelState.UNTESTED
            for bar in after:
                penetrated = (
                    bar.high > price + tolerance
                    if side is LiquiditySide.BUY_SIDE
                    else bar.low < price - tolerance
                )
                if not penetrated:
                    continue
                confirmed = (
                    bar.close < price
                    if side is LiquiditySide.BUY_SIDE
                    else bar.close > price
                )
                invalidated = not confirmed
                confirmation = (
                    PatternConfirmation.CONFIRMED
                    if confirmed
                    else PatternConfirmation.WICK_ONLY
                )
                sweeps.append(
                    LiquiditySweep(
                        sweep_id=_identifier("liquidity-sweep", level_id, bar.bar_id),
                        level_id=level_id,
                        side=side,
                        level_price=price,
                        penetration_price=(bar.high if side is LiquiditySide.BUY_SIDE else bar.low),
                        occurred_at=bar.opened_at,
                        confirmation=confirmation,
                        invalidated=invalidated,
                        invalidation_reason=(
                            "Candle closed beyond the candidate level instead of rejecting it."
                            if invalidated
                            else None
                        ),
                        evidence=(*evidence, _reference(bar)),
                    )
                )
                state = (
                    LiquidityLevelState.SWEPT
                    if confirmed
                    else LiquidityLevelState.INVALIDATED
                )
                break
            levels.append(
                LiquidityLevel(
                    level_id=level_id,
                    side=side,
                    price=price,
                    tolerance=tolerance,
                    touches=len(cluster),
                    formed_at=formed_at,
                    state=state,
                    evidence=evidence,
                )
            )
    return tuple(levels), tuple(sweeps[-limit:])


def _fair_value_gaps(
    bars: tuple[MarketBarEvidence, ...],
    *,
    minimum_gap_bps: float,
    limit: int,
) -> tuple[FairValueGap, ...]:
    gaps: list[FairValueGap] = []
    for index in range(2, len(bars)):
        first, middle, third = bars[index - 2 : index + 1]
        direction: FairValueGapDirection | None = None
        lower = upper = 0.0
        if third.low > first.high:
            direction = FairValueGapDirection.BULLISH
            lower, upper = first.high, third.low
        elif third.high < first.low:
            direction = FairValueGapDirection.BEARISH
            lower, upper = third.high, first.low
        if direction is None:
            continue
        gap_size = upper - lower
        if gap_size < _tolerance((upper + lower) / 2, minimum_gap_bps):
            continue
        state = FairValueGapState.UNTOUCHED
        later = bars[index + 1 :]
        fill_reference: LiquidityBarReference | None = None
        for bar in later:
            if direction is FairValueGapDirection.BULLISH:
                if bar.low <= lower:
                    state = FairValueGapState.FULLY_FILLED
                    fill_reference = _reference(bar)
                    break
                if bar.low < upper:
                    state = FairValueGapState.PARTIALLY_FILLED
                    fill_reference = _reference(bar)
            else:
                if bar.high >= upper:
                    state = FairValueGapState.FULLY_FILLED
                    fill_reference = _reference(bar)
                    break
                if bar.high > lower:
                    state = FairValueGapState.PARTIALLY_FILLED
                    fill_reference = _reference(bar)
        gaps.append(
            FairValueGap(
                gap_id=_identifier("fvg", direction.value, first.bar_id, third.bar_id),
                direction=direction,
                lower_boundary=lower,
                upper_boundary=upper,
                gap_size=gap_size,
                formed_at=third.closed_at,
                state=state,
                invalidated=state is FairValueGapState.FULLY_FILLED,
                evidence=(
                    *tuple(_reference(bar) for bar in (first, middle, third)),
                    *((fill_reference,) if fill_reference is not None else ()),
                ),
            )
        )
    return tuple(gaps[-limit:])


def _structure_breaks(
    bars: tuple[MarketBarEvidence, ...],
    points: tuple[StructurePoint, ...],
    *,
    limit: int,
) -> tuple[StructureBreak, ...]:
    events: list[StructureBreak] = []
    used: set[tuple[str, str]] = set()
    for bar in bars:
        available = tuple(
            point
            for point in points
            if max(item.closed_at for item in point.evidence) <= bar.opened_at
        )
        highs = [item for item in available if item.swing_kind == "high"]
        lows = [item for item in available if item.swing_kind == "low"]
        trend = _structure_trend(available)
        candidates = []
        if highs and bar.high > highs[-1].price:
            candidates.append(("bullish", highs[-1], bar.close > highs[-1].price))
        if lows and bar.low < lows[-1].price:
            candidates.append(("bearish", lows[-1], bar.close < lows[-1].price))
        for direction, reference, close_confirmed in candidates:
            reference_bar = reference.evidence[len(reference.evidence) // 2]
            key = (direction, reference_bar.bar_id)
            if key in used:
                continue
            used.add(key)
            if direction == "bullish":
                kind = (
                    StructureBreakKind.BULLISH_CHOCH
                    if trend is StructureTrend.BEARISH
                    else StructureBreakKind.BULLISH_BOS
                )
                invalidation = lows[-1].price if lows else reference.price
                invalidated = any(item.close < invalidation for item in bars if item.opened_at > bar.opened_at)
                break_price = bar.close if close_confirmed else bar.high
            else:
                kind = (
                    StructureBreakKind.BEARISH_CHOCH
                    if trend is StructureTrend.BULLISH
                    else StructureBreakKind.BEARISH_BOS
                )
                invalidation = highs[-1].price if highs else reference.price
                invalidated = any(item.close > invalidation for item in bars if item.opened_at > bar.opened_at)
                break_price = bar.close if close_confirmed else bar.low
            events.append(
                StructureBreak(
                    break_id=_identifier("structure-break", kind.value, reference_bar.bar_id, bar.bar_id),
                    kind=kind,
                    reference_swing_bar_id=reference_bar.bar_id,
                    reference_level=reference.price,
                    break_price=break_price,
                    occurred_at=bar.closed_at,
                    confirmation=(
                        PatternConfirmation.CONFIRMED
                        if close_confirmed
                        else PatternConfirmation.WICK_ONLY
                    ),
                    invalidation_level=invalidation,
                    invalidated=invalidated,
                    evidence=(*reference.evidence, _reference(bar)),
                )
            )
    return tuple(events[-limit:])


def _order_blocks(
    bars: tuple[MarketBarEvidence, ...],
    breaks: tuple[StructureBreak, ...],
    *,
    limit: int,
) -> tuple[OrderBlockCandidate, ...]:
    index_by_id = {bar.bar_id: index for index, bar in enumerate(bars)}
    candidates: list[OrderBlockCandidate] = []
    for event in breaks:
        if event.confirmation is not PatternConfirmation.CONFIRMED:
            continue
        break_bar = event.evidence[-1]
        break_index = index_by_id.get(break_bar.bar_id)
        if break_index is None:
            continue
        bullish = event.kind in {
            StructureBreakKind.BULLISH_BOS,
            StructureBreakKind.BULLISH_CHOCH,
        }
        source = next(
            (
                bar
                for bar in reversed(bars[:break_index])
                if (bar.close < bar.open if bullish else bar.close > bar.open)
            ),
            None,
        )
        if source is None:
            continue
        lower, upper = source.low, source.high
        state = OrderBlockState.UNMITIGATED
        later = bars[break_index + 1 :]
        for bar in later:
            invalid = bar.close < lower if bullish else bar.close > upper
            overlaps = bar.low <= upper and bar.high >= lower
            if invalid:
                state = OrderBlockState.INVALIDATED
                break
            if overlaps:
                state = OrderBlockState.MITIGATED
        direction = OrderBlockDirection.BULLISH if bullish else OrderBlockDirection.BEARISH
        candidates.append(
            OrderBlockCandidate(
                candidate_id=_identifier("order-block", direction.value, source.bar_id, event.break_id),
                direction=direction,
                source_bar_id=source.bar_id,
                lower_boundary=lower,
                upper_boundary=upper,
                formed_at=source.opened_at,
                associated_break_id=event.break_id,
                state=state,
                invalidation_level=lower if bullish else upper,
                evidence=(_reference(source), *event.evidence),
            )
        )
    return tuple(candidates[-limit:])


def _alignment(
    analyses: tuple[LiquidityTimeframeAnalysis, ...],
    missing: tuple[str, ...],
) -> TimeframeAlignment:
    usable = tuple(
        item
        for item in analyses
        if item.status is not LiquidityAnalysisStatus.INSUFFICIENT_DATA
        and item.freshness == "fresh"
    )
    bullish = tuple(item.timeframe for item in usable if item.structure_trend is StructureTrend.BULLISH)
    bearish = tuple(item.timeframe for item in usable if item.structure_trend is StructureTrend.BEARISH)
    ranges = tuple(item.timeframe for item in usable if item.structure_trend is StructureTrend.RANGE)
    if len(usable) < 2:
        state = TimeframeAlignmentState.INSUFFICIENT_DATA
    elif len(bullish) == len(usable):
        state = TimeframeAlignmentState.ALIGNED_BULLISH
    elif len(bearish) == len(usable):
        state = TimeframeAlignmentState.ALIGNED_BEARISH
    else:
        state = TimeframeAlignmentState.MIXED
    limitations = (
        ("Only validated, fresh, time-aligned series are compared.",)
        if analyses or missing
        else ("No validated timeframe series was available.",)
    )
    return TimeframeAlignment(
        state=state,
        timeframes=tuple(item.timeframe for item in usable),
        bullish_timeframes=bullish,
        bearish_timeframes=bearish,
        range_timeframes=ranges,
        missing_timeframes=missing,
        limitations=limitations,
    )
