"""Frozen contracts for deterministic price-action liquidity intelligence."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.events import AssetClass
from app.models.market_series import MarketBarState


def _normalize_liquidity_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("liquidity timestamps must include a timezone")
    return value.astimezone(UTC)


class LiquidityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiquidityAnalysisStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    STALE = "stale"
    INSUFFICIENT_DATA = "insufficient_data"


class PatternConfirmation(StrEnum):
    CONFIRMED = "confirmed"
    WICK_ONLY = "wick_only"
    PROVISIONAL = "provisional"


class StructureLabel(StrEnum):
    HIGHER_HIGH = "higher_high"
    LOWER_HIGH = "lower_high"
    EQUAL_HIGH = "equal_high"
    HIGHER_LOW = "higher_low"
    LOWER_LOW = "lower_low"
    EQUAL_LOW = "equal_low"
    UNCLASSIFIED = "unclassified"


class StructureScope(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class StructureTrend(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGE = "range"
    INSUFFICIENT_DATA = "insufficient_data"


class LiquiditySide(StrEnum):
    BUY_SIDE = "buy_side"
    SELL_SIDE = "sell_side"


class LiquidityLevelState(StrEnum):
    UNTESTED = "untested"
    SWEPT = "swept"
    INVALIDATED = "invalidated"


class FairValueGapDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class FairValueGapState(StrEnum):
    UNTOUCHED = "untouched"
    PARTIALLY_FILLED = "partially_filled"
    FULLY_FILLED = "fully_filled"


class StructureBreakKind(StrEnum):
    BULLISH_BOS = "bullish_bos"
    BEARISH_BOS = "bearish_bos"
    BULLISH_CHOCH = "bullish_choch"
    BEARISH_CHOCH = "bearish_choch"


class OrderBlockDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class OrderBlockState(StrEnum):
    UNMITIGATED = "unmitigated"
    MITIGATED = "mitigated"
    INVALIDATED = "invalidated"


class TimeframeAlignmentState(StrEnum):
    ALIGNED_BULLISH = "aligned_bullish"
    ALIGNED_BEARISH = "aligned_bearish"
    MIXED = "mixed"
    INSUFFICIENT_DATA = "insufficient_data"


class LiquidityBarReference(LiquidityModel):
    bar_id: str = Field(min_length=1, max_length=120)
    opened_at: datetime
    closed_at: datetime
    open: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    close: FiniteFloat
    state: MarketBarState

    @field_validator("opened_at", "closed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = _normalize_liquidity_timestamp(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def validate_ohlc(self) -> "LiquidityBarReference":
        if self.closed_at <= self.opened_at:
            raise ValueError("bar reference close must follow its open")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar reference high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar reference low is inconsistent")
        return self


class StructurePoint(LiquidityModel):
    swing_kind: Literal["high", "low"]
    label: StructureLabel
    scope: StructureScope
    price: FiniteFloat
    occurred_at: datetime
    confirmation: PatternConfirmation
    detection_method: Literal["strict_symmetric_pivot"] = "strict_symmetric_pivot"
    evidence: tuple[LiquidityBarReference, ...] = Field(min_length=1, max_length=41)

    _normalize_occurred_at = field_validator("occurred_at")(_normalize_liquidity_timestamp)


class LiquidityLevel(LiquidityModel):
    level_id: str
    side: LiquiditySide
    price: FiniteFloat
    tolerance: FiniteFloat = Field(gt=0)
    touches: int = Field(ge=1)
    formed_at: datetime
    state: LiquidityLevelState
    detection_method: Literal["confirmed_swing_cluster"] = "confirmed_swing_cluster"
    evidence: tuple[LiquidityBarReference, ...] = Field(min_length=1, max_length=50)

    _normalize_formed_at = field_validator("formed_at")(_normalize_liquidity_timestamp)


class LiquiditySweep(LiquidityModel):
    sweep_id: str
    level_id: str
    side: LiquiditySide
    level_price: FiniteFloat
    penetration_price: FiniteFloat
    occurred_at: datetime
    confirmation: PatternConfirmation
    invalidated: bool
    invalidation_reason: str | None = Field(default=None, max_length=300)
    detection_method: Literal["level_penetration_with_close_test"] = (
        "level_penetration_with_close_test"
    )
    evidence: tuple[LiquidityBarReference, ...] = Field(min_length=2, max_length=52)

    _normalize_occurred_at = field_validator("occurred_at")(_normalize_liquidity_timestamp)


class FairValueGap(LiquidityModel):
    gap_id: str
    direction: FairValueGapDirection
    lower_boundary: FiniteFloat
    upper_boundary: FiniteFloat
    gap_size: FiniteFloat = Field(gt=0)
    formed_at: datetime
    state: FairValueGapState
    invalidated: bool
    detection_method: Literal["three_candle_imbalance"] = "three_candle_imbalance"
    evidence: tuple[LiquidityBarReference, ...] = Field(min_length=3, max_length=100)

    _normalize_formed_at = field_validator("formed_at")(_normalize_liquidity_timestamp)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "FairValueGap":
        if self.lower_boundary >= self.upper_boundary:
            raise ValueError("FVG lower boundary must be below its upper boundary")
        if abs((self.upper_boundary - self.lower_boundary) - self.gap_size) > 1e-9:
            raise ValueError("FVG size must match its boundaries")
        return self


class StructureBreak(LiquidityModel):
    break_id: str
    kind: StructureBreakKind
    reference_swing_bar_id: str
    reference_level: FiniteFloat
    break_price: FiniteFloat
    occurred_at: datetime
    confirmation: PatternConfirmation
    invalidation_level: FiniteFloat
    invalidated: bool
    detection_method: Literal["prior_swing_close_or_wick_break"] = (
        "prior_swing_close_or_wick_break"
    )
    evidence: tuple[LiquidityBarReference, ...] = Field(min_length=2, max_length=50)

    _normalize_occurred_at = field_validator("occurred_at")(_normalize_liquidity_timestamp)


class OrderBlockCandidate(LiquidityModel):
    candidate_id: str
    direction: OrderBlockDirection
    source_bar_id: str
    lower_boundary: FiniteFloat
    upper_boundary: FiniteFloat
    formed_at: datetime
    associated_break_id: str
    state: OrderBlockState
    invalidation_level: FiniteFloat
    detection_method: Literal["last_opposing_candle_before_confirmed_break"] = (
        "last_opposing_candle_before_confirmed_break"
    )
    evidence: tuple[LiquidityBarReference, ...] = Field(min_length=2, max_length=50)

    _normalize_formed_at = field_validator("formed_at")(_normalize_liquidity_timestamp)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "OrderBlockCandidate":
        if self.lower_boundary > self.upper_boundary:
            raise ValueError("order-block lower boundary cannot exceed its upper boundary")
        return self


class LiquidityTimeframeAnalysis(LiquidityModel):
    status: LiquidityAnalysisStatus
    symbol: str
    asset_class: AssetClass
    venue: str | None = None
    timeframe: str
    provider_id: str
    provider_name: str
    evaluated_at: datetime
    retrieved_at: datetime
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    freshness: Literal["fresh", "stale"]
    completed_candle_count: int = Field(ge=0)
    provisional_candle_count: int = Field(ge=0)
    excluded_future_candle_count: int = Field(ge=0)
    missing_interval_count: int = Field(ge=0)
    structure_trend: StructureTrend
    structure_points: tuple[StructurePoint, ...] = ()
    provisional_structure_points: tuple[StructurePoint, ...] = ()
    liquidity_levels: tuple[LiquidityLevel, ...] = ()
    sweeps: tuple[LiquiditySweep, ...] = ()
    fair_value_gaps: tuple[FairValueGap, ...] = ()
    structure_breaks: tuple[StructureBreak, ...] = ()
    order_block_candidates: tuple[OrderBlockCandidate, ...] = ()
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @field_validator(
        "evaluated_at",
        "retrieved_at",
        "coverage_start",
        "coverage_end",
    )
    @classmethod
    def normalize_analysis_timestamp(cls, value: datetime | None) -> datetime | None:
        return _normalize_liquidity_timestamp(value)


class TimeframeAlignment(LiquidityModel):
    state: TimeframeAlignmentState
    timeframes: tuple[str, ...]
    bullish_timeframes: tuple[str, ...] = ()
    bearish_timeframes: tuple[str, ...] = ()
    range_timeframes: tuple[str, ...] = ()
    missing_timeframes: tuple[str, ...] = ()
    method: Literal["validated_timeframe_structure_comparison"] = (
        "validated_timeframe_structure_comparison"
    )
    limitations: tuple[str, ...] = ()


class LiquidityIntelligenceReport(LiquidityModel):
    calculation_mode: Literal["deterministic_price_action"] = "deterministic_price_action"
    method_version: Literal["liquidity-intelligence/v1"] = "liquidity-intelligence/v1"
    evaluated_at: datetime
    analyses: tuple[LiquidityTimeframeAnalysis, ...] = ()
    alignment: TimeframeAlignment
    available_timeframes: tuple[str, ...] = ()
    unavailable_timeframes: tuple[str, ...] = ()
    institutional_order_flow_observed: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    _normalize_evaluated_at = field_validator("evaluated_at")(_normalize_liquidity_timestamp)
