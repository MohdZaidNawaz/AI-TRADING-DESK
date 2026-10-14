"""Immutable read-only MT5 adapter contracts.

These describe terminal facts and broker constraints only.  They are not
strategy, risk, or order-authority models and contain no credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.portfolio_risk import SourceAccountKey
from app.models.schemas import APIModel


class MT5Model(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MT5SessionState(str, Enum):
    DISCONNECTED = "disconnected"
    INITIALIZING = "initializing"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class MT5ExecutionMode(str, Enum):
    DISABLED = "disabled"
    READ_ONLY = "read_only"
    DRY_RUN = "dry_run"
    LIVE = "live"


class BrokerInstrumentContract(MT5Model):
    """Descriptive broker instrument constraints for one explicit mapping."""

    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    digits: int = Field(ge=0, le=12)
    point: FiniteFloat
    tick_size: FiniteFloat
    tick_value: FiniteFloat | None = None
    contract_size: FiniteFloat | None = None
    minimum_volume: FiniteFloat
    maximum_volume: FiniteFloat
    volume_step: FiniteFloat
    trade_mode: int | None = None
    filling_modes: tuple[str, ...] = ()
    order_modes: tuple[str, ...] = ()
    visible: bool = False
    tradable: bool = False
    quote_available: bool = False

    @field_validator("canonical_symbol", "broker_symbol", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("instrument symbols cannot be blank")
            return normalized
        return value


class MT5AccountSnapshot(MT5Model):
    """Read-only account facts, source-scoped through the existing account key."""

    account: SourceAccountKey
    balance: FiniteFloat
    equity: FiniteFloat
    margin: FiniteFloat
    free_margin: FiniteFloat
    currency: str = Field(min_length=1, max_length=16)
    trading_allowed: bool
    terminal_trade_allowed: bool
    terminal_version: str | None = Field(default=None, max_length=160)


class MT5PositionSnapshot(MT5Model):
    """Read-only broker-reported position; never a new trade instruction."""

    ticket: int
    symbol: str = Field(min_length=1, max_length=80)
    volume: FiniteFloat
    price_open: FiniteFloat
    stop_loss: FiniteFloat | None = None
    take_profit: FiniteFloat | None = None
    profit: FiniteFloat | None = None
    magic: int | None = None
    comment: str | None = Field(default=None, max_length=256)


class MT5OrderStatusSnapshot(MT5Model):
    """Read-only broker-reported order status for future reconciliation."""

    ticket: int
    symbol: str = Field(min_length=1, max_length=80)
    volume_initial: FiniteFloat
    volume_current: FiniteFloat
    price_open: FiniteFloat
    stop_loss: FiniteFloat | None = None
    take_profit: FiniteFloat | None = None
    state: str = Field(min_length=1, max_length=80)
    magic: int | None = None
    comment: str | None = Field(default=None, max_length=256)


class MT5ReadOnlyEvidenceSnapshot(MT5Model):
    """One passive observation from an already-connected MT5 runtime.

    This is deliberately a read-only data transfer object.  Constructing it
    never establishes a terminal session and it contains neither credentials
    nor any broker request capable of changing account state.
    """

    account: MT5AccountSnapshot
    instrument: BrokerInstrumentContract
    bid: FiniteFloat
    ask: FiniteFloat
    quote_observed_at: datetime
    retrieved_at: datetime
    positions: tuple[MT5PositionSnapshot, ...] = ()
    pending_orders: tuple[MT5OrderStatusSnapshot, ...] = ()

    @field_validator("quote_observed_at", "retrieved_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MT5 read-only evidence timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("positions")
    @classmethod
    def stable_positions(
        cls, value: tuple[MT5PositionSnapshot, ...]
    ) -> tuple[MT5PositionSnapshot, ...]:
        if len({item.ticket for item in value}) != len(value):
            raise ValueError("MT5 read-only evidence positions require unique tickets")
        return tuple(sorted(value, key=lambda item: item.ticket))

    @field_validator("pending_orders")
    @classmethod
    def stable_orders(
        cls, value: tuple[MT5OrderStatusSnapshot, ...]
    ) -> tuple[MT5OrderStatusSnapshot, ...]:
        if len({item.ticket for item in value}) != len(value):
            raise ValueError("MT5 read-only evidence pending orders require unique tickets")
        return tuple(sorted(value, key=lambda item: item.ticket))

    @model_validator(mode="after")
    def validate_quote_and_constraints(self) -> "MT5ReadOnlyEvidenceSnapshot":
        if self.bid <= 0 or self.ask <= 0 or self.bid > self.ask:
            raise ValueError("MT5 read-only evidence quote is invalid")
        if self.quote_observed_at > self.retrieved_at:
            raise ValueError("MT5 read-only quote cannot postdate retrieval")
        if (
            self.instrument.minimum_volume <= 0
            or self.instrument.maximum_volume < self.instrument.minimum_volume
            or self.instrument.volume_step <= 0
            or self.instrument.tick_size <= 0
            or self.instrument.point <= 0
        ):
            raise ValueError("MT5 read-only instrument constraints are invalid")
        return self
