"""Typed contracts for deterministic, non-executing historical validation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.risk_strategy_intelligence import RiskSizingContext
from app.models.schemas import AgentAnalysis, Synthesis
from app.models.market_series import canonical_timeframe


class BacktestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"
    UNAVAILABLE = "unavailable"


class BacktestDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class BacktestExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    GAP_STOP = "gap_stop"
    GAP_TARGET = "gap_target"
    MAXIMUM_HOLDING_PERIOD = "maximum_holding_period"
    END_OF_DATA = "end_of_data"


class MetricStatus(StrEnum):
    AVAILABLE = "available"
    UNDEFINED = "undefined"
    INSUFFICIENT_SAMPLE = "insufficient_sample"


class ValidationWindowKind(StrEnum):
    DEVELOPMENT = "development"
    OUT_OF_SAMPLE = "out_of_sample"
    WALK_FORWARD = "walk_forward"


class CostVerificationStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"
    VERIFIED = "verified"
    INVALID = "invalid"


class VerifiedTradingCosts(BacktestModel):
    """Explicit cost facts; absent values are never inferred."""

    spread_price: FiniteFloat | None = Field(default=None, ge=0)
    slippage_price_per_side: FiniteFloat | None = Field(default=None, ge=0)
    commission_per_size_round_trip: FiniteFloat | None = Field(default=None, ge=0)
    source_id: str = Field(min_length=1, max_length=80)
    verified_at: datetime
    verified: Literal[True] = True

    @field_validator("source_id", mode="before")
    @classmethod
    def normalize_source_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("verified_at")
    @classmethod
    def normalize_verified_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cost verification time requires a timezone")
        return value.astimezone(UTC)


class BacktestCostAssessment(BacktestModel):
    status: CostVerificationStatus
    source_id: str | None = Field(default=None, max_length=80)
    verified_at: datetime | None = None
    spread_price: FiniteFloat | None = Field(default=None, ge=0)
    slippage_price_per_side: FiniteFloat | None = Field(default=None, ge=0)
    commission_per_size_round_trip: FiniteFloat | None = Field(default=None, ge=0)
    missing_components: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


class BacktestParameters(BacktestModel):
    strategy_id: Literal["trend_confirmed_scenario_v1"] = "trend_confirmed_scenario_v1"
    development_fraction: float = Field(default=0.7, ge=0.5, le=0.9)
    maximum_holding_bars: int = Field(default=100, ge=1, le=500)
    walk_forward_window_bars: int | None = Field(default=None, ge=25, le=500)
    walk_forward_step_bars: int | None = Field(default=None, ge=1, le=500)
    collision_policy: Literal["stop_first_conservative"] = "stop_first_conservative"
    entry_validity_bars: Literal[1] = 1
    allow_overlapping_positions: Literal[False] = False

    @model_validator(mode="after")
    def validate_walk_forward(self) -> "BacktestParameters":
        if (self.walk_forward_window_bars is None) != (self.walk_forward_step_bars is None):
            raise ValueError("walk-forward window and step must be supplied together")
        return self


class BacktestRequest(BacktestModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: str = Field(min_length=2, max_length=20)
    dataset_id: str | None = Field(default=None, pattern=r"^historical-[0-9a-f]{32}$")
    dataset_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_id: str | None = Field(default=None, min_length=1, max_length=80)
    start_at: datetime | None = None
    end_at: datetime | None = None
    parameters: BacktestParameters = Field(default_factory=BacktestParameters)
    sizing_context: RiskSizingContext | None = None
    verified_costs: VerifiedTradingCosts | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_timeframe(cls, value: object) -> object:
        return canonical_timeframe(value) if isinstance(value, str) else value

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("backtest timestamps require a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_range_and_inputs(self) -> "BacktestRequest":
        if self.start_at is not None and self.end_at is not None and self.start_at >= self.end_at:
            raise ValueError("start_at must be earlier than end_at")
        if self.sizing_context is not None:
            requested = self.symbol.replace("/", "")
            supplied = self.sizing_context.instrument.symbol.replace("/", "").upper()
            if requested.replace("-", "") != supplied.replace("-", ""):
                raise ValueError("sizing context instrument must match the requested symbol")
        if self.dataset_fingerprint is not None and self.dataset_id is None:
            raise ValueError("dataset_fingerprint requires dataset_id")
        return self


class BacktestDataQuality(BacktestModel):
    validated_bar_count: int = Field(ge=0)
    completed_bar_count: int = Field(ge=0)
    provisional_bar_count: int = Field(ge=0)
    duplicate_timestamp_count: int = Field(ge=0)
    missing_interval_count: int = Field(ge=0)
    is_ordered: bool
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None


class BacktestProvenance(BacktestModel):
    series_id: str = Field(min_length=1, max_length=160)
    series_fingerprint: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=80)
    source_name: str = Field(min_length=1, max_length=120)
    retrieved_at: datetime
    timeframe: str
    venue: str | None = None


class BacktestTrade(BacktestModel):
    trade_id: str = Field(min_length=1, max_length=120)
    direction: BacktestDirection
    signal_at: datetime
    entered_at: datetime
    exited_at: datetime
    entry_price: FiniteFloat
    exit_price: FiniteFloat
    stop_loss: FiniteFloat
    take_profit: FiniteFloat
    exit_reason: BacktestExitReason
    holding_bars: int = Field(ge=1)
    holding_seconds: int = Field(ge=1)
    gross_return_pct: FiniteFloat
    net_return_pct: FiniteFloat | None = None
    gross_monetary_pnl: FiniteFloat | None = None
    net_monetary_pnl: FiniteFloat | None = None
    position_size: FiniteFloat | None = Field(default=None, gt=0)
    size_unit: Literal["lots", "contracts", "units"] | None = None
    costs_applied: bool
    entry_bar_id: str
    exit_bar_id: str
    signal_evidence_ids: tuple[str, ...] = ()


class BacktestMetric(BacktestModel):
    status: MetricStatus
    value: FiniteFloat | None = None
    unit: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=240)


class ReturnSeriesMetrics(BacktestModel):
    basis: Literal["gross_returns", "net_returns"]
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    breakeven_trades: int = Field(ge=0)
    win_rate: BacktestMetric
    profit: BacktestMetric
    loss: BacktestMetric
    profit_factor: BacktestMetric
    average_win: BacktestMetric
    average_loss: BacktestMetric
    expectancy: BacktestMetric
    maximum_drawdown: BacktestMetric
    total_return: BacktestMetric
    consecutive_wins: int = Field(ge=0)
    consecutive_losses: int = Field(ge=0)
    sharpe_ratio: BacktestMetric


class PerformanceMetrics(BacktestModel):
    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    breakeven_trades: int = Field(ge=0)
    win_rate: BacktestMetric
    gross_profit: BacktestMetric
    gross_loss: BacktestMetric
    profit_factor: BacktestMetric
    average_win: BacktestMetric
    average_loss: BacktestMetric
    expectancy: BacktestMetric
    maximum_drawdown: BacktestMetric
    total_return: BacktestMetric
    average_holding_duration: BacktestMetric
    consecutive_wins: int = Field(ge=0)
    consecutive_losses: int = Field(ge=0)
    realized_risk_reward: BacktestMetric
    sharpe_ratio: BacktestMetric
    basis: Literal["gross_returns"] = "gross_returns"
    gross: ReturnSeriesMetrics
    net: ReturnSeriesMetrics | None = None


class EquityPoint(BacktestModel):
    timestamp: datetime
    cumulative_return_pct: FiniteFloat
    drawdown_pct: FiniteFloat = Field(le=0)


class ValidationWindow(BacktestModel):
    window_id: str = Field(min_length=1, max_length=80)
    kind: ValidationWindowKind
    start_at: datetime
    end_at: datetime
    observation_count: int = Field(ge=0)
    incomplete_trade_count: int = Field(default=0, ge=0)
    metrics: PerformanceMetrics
    warnings: tuple[str, ...] = ()


class BacktestReport(BacktestModel):
    status: BacktestStatus
    calculation_mode: Literal["deterministic_historical_validation"] = (
        "deterministic_historical_validation"
    )
    method_version: Literal["backtest/v1"] = "backtest/v1"
    strategy_id: Literal["trend_confirmed_scenario_v1"] = "trend_confirmed_scenario_v1"
    symbol: str
    timeframe: str
    evaluated_at: datetime
    data_quality: BacktestDataQuality
    provenance: BacktestProvenance | None = None
    parameters: BacktestParameters
    cost_basis: Literal["verified_net", "gross_only"]
    cost_assessment: BacktestCostAssessment
    trades: tuple[BacktestTrade, ...] = ()
    incomplete_trade_count: int = Field(default=0, ge=0)
    metrics: PerformanceMetrics
    equity_curve: tuple[EquityPoint, ...] = ()
    net_equity_curve: tuple[EquityPoint, ...] = ()
    validation_windows: tuple[ValidationWindow, ...] = ()
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False


class BacktestResponse(BacktestModel):
    report: BacktestReport
    quantitative_specialist: AgentAnalysis
    risk_specialist: AgentAnalysis
    master_synthesis: Synthesis
