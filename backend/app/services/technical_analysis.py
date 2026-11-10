"""Deterministic technical analysis over immutable validated OHLCV evidence."""

from __future__ import annotations

import math

from app.evidence.models import MarketBarEvidence, MarketSeriesEvidence
from app.models.derived_features import (
    DerivedFeatureRequest,
    DerivedMarketFeature,
    FeatureStatus,
    FeatureType,
)
from app.models.market_series import MarketBarState
from app.models.technical_analysis import (
    LevelKind,
    PriceLevelCandidate,
    SwingKind,
    SwingPoint,
    TechnicalAnalysisReport,
    TechnicalCalculationStatus,
    TechnicalTimeframeAnalysis,
    TrendDirection,
)
from app.services.derived_features import (
    DerivedFeatureService,
    feature_request,
    macd_feature_request,
)


TECHNICAL_FEATURE_REQUESTS: tuple[DerivedFeatureRequest, ...] = (
    *(feature_request(FeatureType.SMA, window=window) for window in (20, 50, 200)),
    *(feature_request(FeatureType.EMA, window=window) for window in (20, 50, 200)),
    feature_request(FeatureType.RSI, window=14),
    macd_feature_request(FeatureType.MACD_LINE),
    macd_feature_request(FeatureType.MACD_SIGNAL),
    macd_feature_request(FeatureType.MACD_HISTOGRAM),
    feature_request(FeatureType.ROC, window=14),
    feature_request(FeatureType.ATR, window=14),
    feature_request(FeatureType.ROLLING_STD, window=20),
)

TREND_RULE = (
    "Bullish requires close above SMA20 and EMA20 with SMA20>SMA50>SMA200 and "
    "EMA20>EMA50>EMA200; bearish is the exact inverse; otherwise mixed."
)


class TechnicalAnalysisService:
    """Calculate reproducible indicators and mathematical price structure only."""

    def __init__(
        self,
        *,
        feature_service: DerivedFeatureService | None = None,
        swing_window: int = 2,
        max_levels: int = 5,
    ) -> None:
        if swing_window < 1 or swing_window > 20:
            raise ValueError("swing_window must be between 1 and 20")
        if max_levels < 1 or max_levels > 20:
            raise ValueError("max_levels must be between 1 and 20")
        self._feature_service = feature_service or DerivedFeatureService()
        self._swing_window = swing_window
        self._max_levels = max_levels

    def analyze(
        self,
        series: tuple[MarketSeriesEvidence, ...],
        *,
        requested_timeframes: tuple[str, ...] = (),
    ) -> TechnicalAnalysisReport:
        if any(not isinstance(item, MarketSeriesEvidence) for item in series):
            raise TypeError("series entries must be MarketSeriesEvidence")
        analyses = tuple(
            self._analyze_timeframe(item)
            for item in sorted(series, key=lambda value: value.series_id)
        )
        available = tuple(dict.fromkeys(item.timeframe for item in analyses))
        unavailable = tuple(
            dict.fromkeys(
                timeframe
                for timeframe in requested_timeframes
                if timeframe and timeframe not in available
            )
        )
        return TechnicalAnalysisReport(
            analyses=analyses,
            available_timeframes=available,
            unavailable_timeframes=unavailable,
        )

    def _analyze_timeframe(
        self,
        series: MarketSeriesEvidence,
    ) -> TechnicalTimeframeAnalysis:
        indicators = tuple(
            self._feature_service.calculate(series, request)
            for request in TECHNICAL_FEATURE_REQUESTS
        )
        completed = tuple(
            bar for bar in series.bars if bar.state is MarketBarState.COMPLETED
        )
        contiguous = _contiguous_tail(completed)
        swing_highs, swing_lows = confirmed_swing_points(
            contiguous,
            window=self._swing_window,
        )
        support = tuple(
            PriceLevelCandidate(
                kind=LevelKind.SUPPORT,
                price=point.price,
                source_bar_id=point.bar_id,
                occurred_at=point.occurred_at,
                basis="swing_low",
            )
            for point in swing_lows[-self._max_levels :]
        )
        resistance = tuple(
            PriceLevelCandidate(
                kind=LevelKind.RESISTANCE,
                price=point.price,
                source_bar_id=point.bar_id,
                occurred_at=point.occurred_at,
                basis="swing_high",
            )
            for point in swing_highs[-self._max_levels :]
        )
        trend = _trend_direction(indicators, completed[-1].close if completed else None)
        structure = _structure_observations(swing_highs, swing_lows)
        warnings: list[str] = []
        limitations: list[str] = []
        if series.provisional_count:
            warnings.append("PROVISIONAL_CANDLES_EXCLUDED")
            limitations.append(
                f"Excluded {series.provisional_count} provisional candle(s) from calculations."
            )
        if series.continuity.missing_interval_count:
            warnings.append("MISSING_INTERVALS_PRESENT")
            limitations.append(
                "Price structure uses only the latest contiguous completed-candle segment."
            )
        unavailable = tuple(
            feature for feature in indicators if feature.status is not FeatureStatus.AVAILABLE
        )
        if unavailable:
            warnings.append("INDICATORS_INSUFFICIENT_OR_UNDEFINED")
            limitations.append(
                f"{len(unavailable)} of {len(indicators)} configured indicator values are unavailable."
            )
        if not swing_highs and not swing_lows:
            warnings.append("NO_CONFIRMED_SWING_POINTS")
            limitations.append(
                f"Swing detection requires strict extrema with {self._swing_window} bars on each side."
            )
        available_count = len(indicators) - len(unavailable)
        if available_count == 0 and not swing_highs and not swing_lows:
            status = TechnicalCalculationStatus.INSUFFICIENT_DATA
        elif warnings:
            status = TechnicalCalculationStatus.PARTIAL
        else:
            status = TechnicalCalculationStatus.AVAILABLE
        return TechnicalTimeframeAnalysis(
            status=status,
            symbol=series.symbol,
            asset_class=series.asset_class,
            venue=series.venue,
            timeframe=series.timeframe,
            provider_id=series.provenance.source_id,
            provider_name=series.provenance.source_name,
            retrieved_at=series.provenance.latest_received_at,
            coverage_start=completed[0].opened_at if completed else None,
            coverage_end=completed[-1].closed_at if completed else None,
            validated_candle_count=series.bar_count,
            completed_candle_count=series.completed_count,
            provisional_candle_count=series.provisional_count,
            missing_interval_count=series.continuity.missing_interval_count,
            indicators=indicators,
            trend_direction=trend,
            trend_rule=TREND_RULE,
            swing_window=self._swing_window,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            support_candidates=support,
            resistance_candidates=resistance,
            structure_observations=structure,
            warnings=tuple(warnings),
            limitations=tuple(limitations),
        )


def _contiguous_tail(
    bars: tuple[MarketBarEvidence, ...],
) -> tuple[MarketBarEvidence, ...]:
    if len(bars) < 2:
        return bars
    start = 0
    for index, (previous, current) in enumerate(
        zip(bars, bars[1:], strict=False),
        start=1,
    ):
        if previous.closed_at != current.opened_at:
            start = index
    return bars[start:]


def confirmed_swing_points(
    bars: tuple[MarketBarEvidence, ...],
    *,
    window: int,
) -> tuple[tuple[SwingPoint, ...], tuple[SwingPoint, ...]]:
    highs: list[SwingPoint] = []
    lows: list[SwingPoint] = []
    for index in range(window, len(bars) - window):
        current = bars[index]
        left = bars[index - window : index]
        right = bars[index + 1 : index + window + 1]
        neighbours = (*left, *right)
        if all(current.high > bar.high for bar in neighbours):
            highs.append(
                SwingPoint(
                    kind=SwingKind.HIGH,
                    bar_id=current.bar_id,
                    occurred_at=current.opened_at,
                    price=current.high,
                    left_bars=window,
                    right_bars=window,
                )
            )
        if all(current.low < bar.low for bar in neighbours):
            lows.append(
                SwingPoint(
                    kind=SwingKind.LOW,
                    bar_id=current.bar_id,
                    occurred_at=current.opened_at,
                    price=current.low,
                    left_bars=window,
                    right_bars=window,
                )
            )
    return tuple(highs), tuple(lows)


# Private compatibility alias for existing internal callers.
_swing_points = confirmed_swing_points


def _trend_direction(
    indicators: tuple[DerivedMarketFeature, ...],
    latest_close: float | None,
) -> TrendDirection:
    values: dict[tuple[FeatureType, int], float] = {}
    for feature in indicators:
        if feature.status is not FeatureStatus.AVAILABLE or feature.value is None:
            continue
        parameters = {item.name: item.value for item in feature.parameters}
        window = parameters.get("window")
        if isinstance(window, int):
            values[(feature.feature_type, window)] = feature.value
    required = tuple(
        (feature_type, window)
        for feature_type in (FeatureType.SMA, FeatureType.EMA)
        for window in (20, 50, 200)
    )
    if latest_close is None or any(key not in values for key in required):
        return TrendDirection.INSUFFICIENT_DATA
    sma20, sma50, sma200 = (values[(FeatureType.SMA, value)] for value in (20, 50, 200))
    ema20, ema50, ema200 = (values[(FeatureType.EMA, value)] for value in (20, 50, 200))
    if latest_close > sma20 and latest_close > ema20 and sma20 > sma50 > sma200 and ema20 > ema50 > ema200:
        return TrendDirection.BULLISH
    if latest_close < sma20 and latest_close < ema20 and sma20 < sma50 < sma200 and ema20 < ema50 < ema200:
        return TrendDirection.BEARISH
    return TrendDirection.MIXED


def _structure_observations(
    highs: tuple[SwingPoint, ...],
    lows: tuple[SwingPoint, ...],
) -> tuple[str, ...]:
    observations: list[str] = []
    if len(highs) >= 2:
        previous, current = highs[-2:]
        if math.isclose(current.price, previous.price, rel_tol=1e-12, abs_tol=1e-12):
            observations.append("EQUAL_HIGHS")
        elif current.price > previous.price:
            observations.append("HIGHER_HIGH")
        else:
            observations.append("LOWER_HIGH")
    if len(lows) >= 2:
        previous, current = lows[-2:]
        if math.isclose(current.price, previous.price, rel_tol=1e-12, abs_tol=1e-12):
            observations.append("EQUAL_LOWS")
        elif current.price > previous.price:
            observations.append("HIGHER_LOW")
        else:
            observations.append("LOWER_LOW")
    return tuple(observations)
