"""Deterministic calculations over immutable provider-neutral market series."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.evidence.models import MarketBarEvidence, MarketSeriesEvidence
from app.models.derived_features import (
    DerivedFeatureRequest,
    DerivedFeatureSnapshot,
    DerivedMarketFeature,
    FeatureParameter,
    FeatureQualityFlag,
    FeatureStatus,
    FeatureType,
    FeatureValueUnit,
    feature_identity,
    feature_snapshot_identity,
)
from app.models.market_series import MarketBarState


METHOD_VERSION = "1.0.0"

# Parameterized calculations require an explicit internal request. These safe
# defaults need no arbitrary lookback convention and do not imply a stance.
DEFAULT_DERIVED_FEATURE_REQUESTS: tuple[DerivedFeatureRequest, ...] = (
    DerivedFeatureRequest(feature_type=FeatureType.BAR_RANGE),
    DerivedFeatureRequest(feature_type=FeatureType.SIMPLE_RETURN),
    DerivedFeatureRequest(feature_type=FeatureType.LOG_RETURN),
)


@dataclass(frozen=True, slots=True)
class _FeatureSpecification:
    method_id: str
    value_unit: FeatureValueUnit
    required_bar_count: int


@dataclass(frozen=True, slots=True)
class _PreparedWindow:
    bars: tuple[MarketBarEvidence, ...]
    quality_flags: tuple[FeatureQualityFlag, ...]
    status: FeatureStatus | None
    limitations: tuple[str, ...]


class DerivedFeatureService:
    """Calculate approved Phase 5B-1 features without side effects or I/O."""

    def calculate(
        self,
        series: MarketSeriesEvidence,
        request: DerivedFeatureRequest,
    ) -> DerivedMarketFeature:
        if not isinstance(series, MarketSeriesEvidence):
            raise TypeError("series must be a MarketSeriesEvidence")
        if not isinstance(request, DerivedFeatureRequest):
            raise TypeError("request must be a DerivedFeatureRequest")

        specification = _feature_specification(request)
        prepared = _prepare_window(series, specification.required_bar_count)
        status = prepared.status
        value: float | None = None
        limitations = list(prepared.limitations)
        quality_flags = set(prepared.quality_flags)

        if status is None:
            try:
                status, value, calculation_limitation = _calculate_value(
                    request.feature_type,
                    prepared.bars,
                )
            except (ArithmeticError, ValueError):
                status = FeatureStatus.REJECTED_INPUT
                value = None
                calculation_limitation = "The derived calculation could not produce a finite result."
            if calculation_limitation is not None:
                limitations.append(calculation_limitation)

        if prepared.bars and _is_flat_input(request.feature_type, prepared.bars):
            quality_flags.add(FeatureQualityFlag.FLAT_SERIES)

        if status is FeatureStatus.AVAILABLE and (
            value is None or not math.isfinite(value)
        ):
            status = FeatureStatus.REJECTED_INPUT
            value = None
            limitations.append("The derived result was not finite.")

        first_bar = prepared.bars[0] if prepared.bars else None
        last_bar = prepared.bars[-1] if prepared.bars else None
        ordered_flags = tuple(sorted(quality_flags, key=lambda item: item.value))
        ordered_limitations = tuple(sorted(set(limitations)))
        feature_id = feature_identity(
            feature_type=request.feature_type,
            parameters=request.parameters,
            method_id=specification.method_id,
            method_version=METHOD_VERSION,
            input_series_id=series.series_id,
            input_series_fingerprint=series.fingerprint,
            input_first_bar_id=first_bar.bar_id if first_bar is not None else None,
            input_last_bar_id=last_bar.bar_id if last_bar is not None else None,
        )
        return DerivedMarketFeature(
            feature_id=feature_id,
            feature_type=request.feature_type,
            status=status or FeatureStatus.REJECTED_INPUT,
            symbol=series.symbol,
            asset_class=series.asset_class,
            venue=series.venue,
            timeframe=series.timeframe,
            as_of=last_bar.closed_at if last_bar is not None else None,
            value=value,
            value_unit=specification.value_unit,
            parameters=request.parameters,
            method_id=specification.method_id,
            method_version=METHOD_VERSION,
            input_series_id=series.series_id,
            input_series_fingerprint=series.fingerprint,
            input_first_bar_id=first_bar.bar_id if first_bar is not None else None,
            input_last_bar_id=last_bar.bar_id if last_bar is not None else None,
            input_bar_count=len(prepared.bars),
            required_bar_count=specification.required_bar_count,
            completed_only=True,
            quality_flags=ordered_flags,
            limitations=ordered_limitations,
        )

    def calculate_snapshot(
        self,
        series: tuple[MarketSeriesEvidence, ...],
        requests: tuple[DerivedFeatureRequest, ...],
    ) -> DerivedFeatureSnapshot:
        if any(not isinstance(item, MarketSeriesEvidence) for item in series):
            raise TypeError("series entries must be MarketSeriesEvidence")
        if any(not isinstance(item, DerivedFeatureRequest) for item in requests):
            raise TypeError("requests must contain DerivedFeatureRequest values")

        ordered_series = tuple(sorted(series, key=lambda item: item.series_id))
        ordered_requests = _canonical_requests(requests)
        features = tuple(
            self.calculate(item, request)
            for item in ordered_series
            for request in ordered_requests
        )
        return DerivedFeatureSnapshot(
            snapshot_id=feature_snapshot_identity(features),
            features=features,
        )


def feature_request(
    feature_type: FeatureType,
    *,
    window: int | None = None,
) -> DerivedFeatureRequest:
    """Build one explicit internal request with canonical parameters."""
    parameters = (
        (FeatureParameter(name="window", value=window),)
        if window is not None
        else ()
    )
    return DerivedFeatureRequest(feature_type=feature_type, parameters=parameters)


def _feature_specification(request: DerivedFeatureRequest) -> _FeatureSpecification:
    no_window = {
        FeatureType.BAR_RANGE: ("friday.bar_range", FeatureValueUnit.PRICE, 1),
        FeatureType.SIMPLE_RETURN: ("friday.simple_close_return", FeatureValueUnit.RATIO, 2),
        FeatureType.LOG_RETURN: ("friday.natural_log_close_return", FeatureValueUnit.RATIO, 2),
    }
    windowed = {
        FeatureType.AVERAGE_RANGE: ("friday.average_range_arithmetic_mean", FeatureValueUnit.PRICE),
        FeatureType.SMA: ("friday.simple_moving_average", FeatureValueUnit.PRICE),
        FeatureType.ROC: ("friday.close_rate_of_change", FeatureValueUnit.RATIO),
        FeatureType.ROLLING_STD: ("friday.population_std_simple_returns", FeatureValueUnit.RATIO),
    }

    if request.feature_type in no_window:
        if request.parameters:
            raise ValueError(f"{request.feature_type.value} does not accept parameters")
        method_id, unit, required = no_window[request.feature_type]
        return _FeatureSpecification(method_id, unit, required)

    if request.feature_type not in windowed:  # pragma: no cover - enum exhaustiveness guard
        raise ValueError(f"unsupported derived feature: {request.feature_type.value}")
    if len(request.parameters) != 1 or request.parameters[0].name != "window":
        raise ValueError(f"{request.feature_type.value} requires exactly one window parameter")
    window = request.parameters[0].value
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ValueError("window must be a positive integer")

    method_id, unit = windowed[request.feature_type]
    required = window + 1 if request.feature_type in {
        FeatureType.ROC,
        FeatureType.ROLLING_STD,
    } else window
    return _FeatureSpecification(method_id, unit, required)


def _prepare_window(
    series: MarketSeriesEvidence,
    required_bar_count: int,
) -> _PreparedWindow:
    completed = tuple(
        bar for bar in series.bars if bar.state is MarketBarState.COMPLETED
    )
    selected = (
        completed[-required_bar_count:]
        if len(completed) >= required_bar_count
        else completed
    )
    flags: set[FeatureQualityFlag] = set()
    limitations: list[str] = []

    if any(bar.state is MarketBarState.PROVISIONAL for bar in series.bars):
        flags.add(FeatureQualityFlag.PROVISIONAL_EXCLUDED)
    if selected:
        flags.add(FeatureQualityFlag.STALE_INPUT_UNASSESSED)

    invalid = any(not _valid_bar(bar) for bar in selected)
    gap_in_window = _has_gap(selected)
    if gap_in_window:
        flags.add(FeatureQualityFlag.GAP_DETECTED)

    if invalid:
        limitations.append("The selected input window contains invalid or non-finite values.")
        return _PreparedWindow(
            bars=selected,
            quality_flags=tuple(flags),
            status=FeatureStatus.REJECTED_INPUT,
            limitations=tuple(limitations),
        )

    if len(selected) < required_bar_count:
        limitations.append(
            f"Requires {required_bar_count} completed contiguous bars; {len(selected)} were available."
        )
        return _PreparedWindow(
            bars=selected,
            quality_flags=tuple(flags),
            status=FeatureStatus.INSUFFICIENT_DATA,
            limitations=tuple(limitations),
        )

    if gap_in_window:
        limitations.append("A gap exists inside the required calculation window.")
        return _PreparedWindow(
            bars=selected,
            quality_flags=tuple(flags),
            status=FeatureStatus.INSUFFICIENT_DATA,
            limitations=tuple(limitations),
        )

    if _has_earlier_gap(completed, selected[0]):
        flags.add(FeatureQualityFlag.EARLIER_GAP_EXCLUDED)
    return _PreparedWindow(
        bars=selected,
        quality_flags=tuple(flags),
        status=None,
        limitations=(),
    )


def _calculate_value(
    feature_type: FeatureType,
    bars: tuple[MarketBarEvidence, ...],
) -> tuple[FeatureStatus, float | None, str | None]:
    if feature_type is FeatureType.BAR_RANGE:
        return FeatureStatus.AVAILABLE, bars[-1].high - bars[-1].low, None
    if feature_type is FeatureType.AVERAGE_RANGE:
        value = math.fsum(bar.high - bar.low for bar in bars) / len(bars)
        return FeatureStatus.AVAILABLE, value, None
    if feature_type is FeatureType.SMA:
        return FeatureStatus.AVAILABLE, math.fsum(bar.close for bar in bars) / len(bars), None
    if feature_type is FeatureType.SIMPLE_RETURN:
        return _ratio_change(bars[-2].close, bars[-1].close)
    if feature_type is FeatureType.LOG_RETURN:
        previous = bars[-2].close
        current = bars[-1].close
        if previous <= 0 or current <= 0:
            return (
                FeatureStatus.UNDEFINED,
                None,
                "Log return requires two positive close values.",
            )
        return FeatureStatus.AVAILABLE, math.log(current / previous), None
    if feature_type is FeatureType.ROC:
        return _ratio_change(bars[0].close, bars[-1].close)
    if feature_type is FeatureType.ROLLING_STD:
        returns: list[float] = []
        for previous, current in zip(bars, bars[1:], strict=False):
            if previous.close == 0:
                return (
                    FeatureStatus.UNDEFINED,
                    None,
                    "Rolling return standard deviation is undefined with a zero base close.",
                )
            returns.append(current.close / previous.close - 1.0)
        mean = math.fsum(returns) / len(returns)
        variance = math.fsum((value - mean) ** 2 for value in returns) / len(returns)
        return FeatureStatus.AVAILABLE, math.sqrt(variance), None
    raise ValueError(f"unsupported derived feature: {feature_type.value}")  # pragma: no cover


def _ratio_change(base: float, current: float) -> tuple[FeatureStatus, float | None, str | None]:
    if base == 0:
        return FeatureStatus.UNDEFINED, None, "Ratio change is undefined with a zero base close."
    return FeatureStatus.AVAILABLE, current / base - 1.0, None


def _valid_bar(bar: MarketBarEvidence) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close)
    return (
        all(math.isfinite(value) for value in values)
        and (bar.volume is None or (math.isfinite(bar.volume) and bar.volume >= 0))
        and bar.high >= max(bar.open, bar.close, bar.low)
        and bar.low <= min(bar.open, bar.close, bar.high)
        and bar.closed_at > bar.opened_at
    )


def _has_gap(bars: tuple[MarketBarEvidence, ...]) -> bool:
    return any(
        previous.closed_at != current.opened_at
        for previous, current in zip(bars, bars[1:], strict=False)
    )


def _has_earlier_gap(
    completed: tuple[MarketBarEvidence, ...],
    first_selected: MarketBarEvidence,
) -> bool:
    return any(
        previous.closed_at != current.opened_at
        and current.opened_at <= first_selected.opened_at
        for previous, current in zip(completed, completed[1:], strict=False)
    )


def _is_flat_input(
    feature_type: FeatureType,
    bars: tuple[MarketBarEvidence, ...],
) -> bool:
    if feature_type in {FeatureType.BAR_RANGE, FeatureType.AVERAGE_RANGE}:
        return all(bar.high == bar.low for bar in bars)
    return len(bars) > 1 and all(bar.close == bars[0].close for bar in bars[1:])


def _canonical_requests(
    requests: tuple[DerivedFeatureRequest, ...],
) -> tuple[DerivedFeatureRequest, ...]:
    unique: dict[tuple[str, tuple[tuple[str, str], ...]], DerivedFeatureRequest] = {}
    for request in requests:
        key = (
            request.feature_type.value,
            tuple((item.name, repr(item.value)) for item in request.parameters),
        )
        unique[key] = request
    return tuple(unique[key] for key in sorted(unique))
