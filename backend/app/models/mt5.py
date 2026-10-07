"""Immutable read-only MT5 adapter contracts.

These describe terminal facts and broker constraints only.  They are not
strategy, risk, or order-authority models and contain no credentials.
"""

from __future__ import annotations

from enum import Enum

from pydantic import ConfigDict, Field, FiniteFloat, field_validator

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
