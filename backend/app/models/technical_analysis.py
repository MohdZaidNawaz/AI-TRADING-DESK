"""Deterministic technical-analysis results derived only from validated bars."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from app.models.derived_features import DerivedMarketFeature
from app.models.events import AssetClass


class TechnicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TechnicalCalculationStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"


class TrendDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    MIXED = "mixed"
    INSUFFICIENT_DATA = "insufficient_data"


class SwingKind(StrEnum):
    HIGH = "high"
    LOW = "low"


class LevelKind(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


class SwingPoint(TechnicalModel):
    kind: SwingKind
    bar_id: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    price: FiniteFloat
    left_bars: int = Field(ge=1, le=20)
    right_bars: int = Field(ge=1, le=20)


class PriceLevelCandidate(TechnicalModel):
    kind: LevelKind
    price: FiniteFloat
    source_bar_id: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    touches: Literal[1] = 1
    basis: Literal["swing_high", "swing_low"]


class TechnicalTimeframeAnalysis(TechnicalModel):
    status: TechnicalCalculationStatus
    symbol: str = Field(min_length=1, max_length=40)
    asset_class: AssetClass
    venue: str | None = Field(default=None, max_length=80)
    timeframe: str = Field(min_length=1, max_length=20)
    provider_id: str = Field(min_length=1, max_length=80)
    provider_name: str = Field(min_length=1, max_length=120)
    retrieved_at: datetime
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    validated_candle_count: int = Field(ge=0)
    completed_candle_count: int = Field(ge=0)
    provisional_candle_count: int = Field(ge=0)
    missing_interval_count: int = Field(ge=0)
    indicators: tuple[DerivedMarketFeature, ...] = ()
    trend_direction: TrendDirection
    trend_rule: str = Field(min_length=1, max_length=500)
    swing_window: int = Field(ge=1, le=20)
    swing_highs: tuple[SwingPoint, ...] = ()
    swing_lows: tuple[SwingPoint, ...] = ()
    support_candidates: tuple[PriceLevelCandidate, ...] = ()
    resistance_candidates: tuple[PriceLevelCandidate, ...] = ()
    structure_observations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @field_validator("warnings", "limitations", "structure_observations")
    @classmethod
    def canonicalize_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


class TechnicalAnalysisReport(TechnicalModel):
    calculation_mode: Literal["deterministic"] = "deterministic"
    method_version: Literal["technical-analysis/v1"] = "technical-analysis/v1"
    analyses: tuple[TechnicalTimeframeAnalysis, ...] = ()
    available_timeframes: tuple[str, ...] = ()
    unavailable_timeframes: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False
