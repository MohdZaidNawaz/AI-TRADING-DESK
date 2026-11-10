"""Immutable contracts for deterministic risk and strategy research."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.events import AssetClass


class RiskStrategyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskStrategyStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    STALE = "stale"
    INSUFFICIENT_DATA = "insufficient_data"


class CalculationStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class ScenarioDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class ScenarioStatus(StrEnum):
    CONDITIONAL = "conditional"
    NO_TRADE = "no_trade"
    INSUFFICIENT_DATA = "insufficient_data"


class EvidenceSufficiency(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class VolatilityRegime(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"


class InstrumentRiskSpecification(RiskStrategyModel):
    """Explicit verified contract facts; never populated from symbol conventions."""

    symbol: str = Field(min_length=1, max_length=40)
    asset_class: AssetClass
    size_unit: Literal["lots", "contracts", "units"]
    contract_size: FiniteFloat | None = Field(default=None, gt=0)
    minimum_size: FiniteFloat | None = Field(default=None, gt=0)
    maximum_size: FiniteFloat | None = Field(default=None, gt=0)
    size_step: FiniteFloat | None = Field(default=None, gt=0)
    tick_size: FiniteFloat | None = Field(default=None, gt=0)
    tick_value: FiniteFloat | None = Field(default=None, gt=0)
    tick_value_currency: str | None = Field(default=None, min_length=3, max_length=16)
    pip_size: FiniteFloat | None = Field(default=None, gt=0)
    source_id: str = Field(min_length=1, max_length=80)
    verified_at: datetime
    verified: Literal[True] = True

    @field_validator("verified_at")
    @classmethod
    def normalize_verified_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("instrument specification timestamp requires a timezone")
        return value.astimezone(UTC)

    @field_validator("tick_value_currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_bounds(self) -> "InstrumentRiskSpecification":
        values = (self.minimum_size, self.maximum_size, self.size_step)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("size bounds require minimum, maximum, and step")
        if (
            self.minimum_size is not None
            and self.maximum_size is not None
            and self.maximum_size < self.minimum_size
        ):
            raise ValueError("maximum size cannot be below minimum size")
        if (self.tick_size is None) != (self.tick_value is None):
            raise ValueError("tick size and tick value must be supplied together")
        if self.tick_value is not None and self.tick_value_currency is None:
            raise ValueError("tick value requires its monetary currency")
        return self


class AccountRiskInput(RiskStrategyModel):
    equity: FiniteFloat = Field(gt=0)
    risk_percentage: FiniteFloat = Field(gt=0, le=100)
    currency: str = Field(min_length=3, max_length=16)
    observed_at: datetime
    source_id: str = Field(min_length=1, max_length=80)
    verified: Literal[True] = True

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("account observation timestamp requires a timezone")
        return value.astimezone(UTC)


class CurrencyConversionInput(RiskStrategyModel):
    from_currency: str = Field(min_length=3, max_length=16)
    to_currency: str = Field(min_length=3, max_length=16)
    rate: FiniteFloat = Field(gt=0)
    observed_at: datetime
    source_id: str = Field(min_length=1, max_length=80)
    verified: Literal[True] = True

    @field_validator("from_currency", "to_currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("conversion timestamp requires a timezone")
        return value.astimezone(UTC)


class RiskSizingContext(RiskStrategyModel):
    account: AccountRiskInput
    instrument: InstrumentRiskSpecification
    conversion: CurrencyConversionInput | None = None
    verified_spread: FiniteFloat | None = Field(default=None, ge=0)
    verified_commission_per_size: FiniteFloat | None = Field(default=None, ge=0)


class EntryZone(RiskStrategyModel):
    lower: FiniteFloat
    upper: FiniteFloat
    reference_price: FiniteFloat
    basis: str = Field(min_length=1, max_length=160)
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_zone(self) -> "EntryZone":
        if self.lower > self.upper:
            raise ValueError("entry-zone lower boundary cannot exceed upper boundary")
        if not self.lower <= self.reference_price <= self.upper:
            raise ValueError("entry reference must lie inside its zone")
        return self


class RiskRewardAssessment(RiskStrategyModel):
    status: CalculationStatus
    entry_price: FiniteFloat | None = None
    stop_loss: FiniteFloat | None = None
    invalidation_level: FiniteFloat | None = None
    target_price: FiniteFloat | None = None
    stop_distance_price: FiniteFloat | None = Field(default=None, gt=0)
    stop_distance_pips: FiniteFloat | None = Field(default=None, gt=0)
    stop_distance_atr_multiple: FiniteFloat | None = Field(default=None, gt=0)
    stop_suitability: str = Field(min_length=1, max_length=240)
    potential_reward: FiniteFloat | None = Field(default=None, gt=0)
    ratio: FiniteFloat | None = Field(default=None, gt=0)
    limitations: tuple[str, ...] = ()


class PositionSizingResult(RiskStrategyModel):
    status: CalculationStatus
    account_currency: str | None = Field(default=None, max_length=16)
    risk_percentage: FiniteFloat | None = None
    risk_amount: FiniteFloat | None = Field(default=None, ge=0)
    position_size: FiniteFloat | None = Field(default=None, gt=0)
    size_unit: Literal["lots", "contracts", "units"] | None = None
    units: FiniteFloat | None = Field(default=None, gt=0)
    monetary_loss_per_size: FiniteFloat | None = Field(default=None, gt=0)
    estimated_transaction_cost: FiniteFloat | None = Field(default=None, ge=0)
    assumptions: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class VolatilityAssessment(RiskStrategyModel):
    status: CalculationStatus
    timeframe: str | None = Field(default=None, max_length=20)
    atr: FiniteFloat | None = Field(default=None, ge=0)
    realized_volatility: FiniteFloat | None = Field(default=None, ge=0)
    atr_percentage_of_price: FiniteFloat | None = Field(default=None, ge=0)
    latest_range_to_atr: FiniteFloat | None = Field(default=None, ge=0)
    regime: VolatilityRegime = VolatilityRegime.UNKNOWN
    range_expansion: bool | None = None
    stop_distance_atr_multiple: FiniteFloat | None = Field(default=None, ge=0)
    stop_suitability: str = Field(min_length=1, max_length=240)
    warnings: tuple[str, ...] = ()


class StrategyQualityFactor(RiskStrategyModel):
    factor: str = Field(min_length=1, max_length=80)
    weight: int = Field(ge=0, le=100)
    score_fraction: FiniteFloat = Field(ge=0, le=1)
    points: FiniteFloat = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=300)


class StrategyQualityAssessment(RiskStrategyModel):
    score: FiniteFloat = Field(ge=0, le=100)
    maximum_score: Literal[100] = 100
    sufficiency: EvidenceSufficiency
    factors: tuple[StrategyQualityFactor, ...]
    interpretation: Literal[
        "deterministic evidence-quality score; not a win probability or profitability forecast"
    ] = "deterministic evidence-quality score; not a win probability or profitability forecast"


class StrategyScenario(RiskStrategyModel):
    direction: ScenarioDirection
    status: ScenarioStatus
    entry_zone: EntryZone | None = None
    entry_conditions: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    stop_loss_candidate: FiniteFloat | None = None
    target_candidates: tuple[FiniteFloat, ...] = ()
    risk_reward: RiskRewardAssessment
    position_sizing: PositionSizingResult
    quality: StrategyQualityAssessment
    supporting_evidence: tuple[str, ...] = ()
    conflicting_evidence: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    expiration_conditions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class RiskStrategyIntelligenceReport(RiskStrategyModel):
    analysis_mode: Literal["deterministic_risk_strategy_research"] = (
        "deterministic_risk_strategy_research"
    )
    method_version: Literal["risk-strategy/v1"] = "risk-strategy/v1"
    subject: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    status: RiskStrategyStatus
    volatility: VolatilityAssessment
    scenarios: tuple[StrategyScenario, ...]
    selected_scenario: Literal["none"] = "none"
    warnings: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("risk-strategy evaluation time requires a timezone")
        return value.astimezone(UTC)
