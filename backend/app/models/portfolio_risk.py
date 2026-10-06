"""Immutable provider-neutral source-reported portfolio and account-risk evidence.

This module preserves reported account state, positions, and limits as facts. It
does not calculate exposure, leverage, drawdown, sizing, or trading decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.economic_events import EconomicValue, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.schemas import APIModel


class PortfolioRiskModel(APIModel):
    """Frozen base for portfolio/account-risk boundary contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class ReportedPositionSide(str, Enum):
    """A source-reported position side, never a FRIDAY trade instruction."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    UNKNOWN = "unknown"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class RiskLimitKind(str, Enum):
    MAXIMUM_DRAWDOWN = "maximum_drawdown"
    DAILY_LOSS = "daily_loss"
    MARGIN = "margin"
    EXPOSURE = "exposure"
    POSITION = "position"
    OTHER = "other"


class PortfolioRiskSource(PortfolioRiskModel):
    """Immutable source identity for source-reported account/risk facts."""

    source_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    transport: SourceTransport
    domain: DataDomain
    origin_id: str | None = Field(default=None, max_length=80)
    origin_name: str | None = Field(default=None, max_length=120)

    @field_validator("source_id", "display_name", "origin_id", "origin_name", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_source(self) -> "PortfolioRiskSource":
        if self.domain is not DataDomain.ALTERNATIVE_DATA:
            raise ValueError("portfolio-risk sources must use the alternative_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("portfolio-risk source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class SourceAccountKey(PortfolioRiskModel):
    """Source-scoped account identity without cross-provider reconciliation."""

    source_id: str = Field(min_length=1, max_length=80)
    source_account_id: str = Field(min_length=1, max_length=160)
    account_reference: str | None = Field(default=None, max_length=160)

    @field_validator("source_id", "source_account_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("source account identity values cannot be blank")
            return normalized
        return value

    @field_validator("account_reference", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @computed_field(return_type=str)
    @property
    def account_id(self) -> str:
        return _stable_id(
            "portfolio-risk-account",
            self.source_id,
            self.source_account_id,
            self.account_reference or "",
        )


class ReportedAccountState(PortfolioRiskModel):
    """Explicit account facts, with no implied exposure or risk calculations."""

    currency: str | None = Field(default=None, max_length=12)
    balance: EconomicValue | None = None
    equity: EconomicValue | None = None
    reference_equity: EconomicValue | None = None
    available_funds: EconomicValue | None = None
    margin_used: EconomicValue | None = None
    margin_available: EconomicValue | None = None
    unrealized_pnl: EconomicValue | None = None
    realized_pnl: EconomicValue | None = None
    daily_loss: EconomicValue | None = None
    status: AccountStatus = AccountStatus.UNKNOWN

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_money_values(self) -> "ReportedAccountState":
        values = (
            self.balance,
            self.equity,
            self.reference_equity,
            self.available_funds,
            self.margin_used,
            self.margin_available,
            self.unrealized_pnl,
            self.realized_pnl,
            self.daily_loss,
        )
        if any(value is not None and value.unit is not EconomicValueUnit.CURRENCY for value in values):
            raise ValueError("reported account monetary values must use the currency unit")
        if any(value is not None for value in values) and self.currency is None:
            raise ValueError("reported account monetary values require an explicit currency")
        return self


class ReportedPortfolioPosition(PortfolioRiskModel):
    """One source-reported position without inferred exposure or execution meaning."""

    source_position_id: str = Field(min_length=1, max_length=160)
    source_instrument_id: str = Field(min_length=1, max_length=160)
    source_symbol: str | None = Field(default=None, max_length=40)
    venue: str | None = Field(default=None, max_length=80)
    reported_side: ReportedPositionSide = ReportedPositionSide.UNKNOWN
    quantity: EconomicValue
    price_currency: str | None = Field(default=None, max_length=12)
    average_entry_price: EconomicValue | None = None
    reference_price: EconomicValue | None = None
    realized_pnl: EconomicValue | None = None
    unrealized_pnl: EconomicValue | None = None
    pnl_currency: str | None = Field(default=None, max_length=12)
    status: PositionStatus = PositionStatus.UNKNOWN
    opened_at: datetime | None = None

    @field_validator("source_position_id", "source_instrument_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("source position identity values cannot be blank")
            return normalized
        return value

    @field_validator("source_symbol", "venue", "price_currency", "pnl_currency", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("price_currency", "pnl_currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("opened_at")
    @classmethod
    def normalize_opened_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("opened_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_reported_values(self) -> "ReportedPortfolioPosition":
        if self.quantity.unit is EconomicValueUnit.CURRENCY:
            raise ValueError("reported position quantity cannot use the currency unit")
        prices = (self.average_entry_price, self.reference_price)
        if any(value is not None and value.unit is not EconomicValueUnit.CURRENCY for value in prices):
            raise ValueError("reported position prices must use the currency unit")
        if any(value is not None for value in prices) and self.price_currency is None:
            raise ValueError("reported position prices require an explicit price_currency")
        pnl_values = (self.realized_pnl, self.unrealized_pnl)
        if any(value is not None and value.unit is not EconomicValueUnit.CURRENCY for value in pnl_values):
            raise ValueError("reported position P&L values must use the currency unit")
        if any(value is not None for value in pnl_values) and self.pnl_currency is None:
            raise ValueError("reported position P&L values require an explicit pnl_currency")
        return self


class ReportedRiskLimit(PortfolioRiskModel):
    """One explicitly source-reported limit, without policy interpretation."""

    source_limit_id: str = Field(min_length=1, max_length=160)
    kind: RiskLimitKind
    value: EconomicValue
    label: str | None = Field(default=None, max_length=240)

    @field_validator("source_limit_id", mode="before")
    @classmethod
    def normalize_limit_id(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("source_limit_id cannot be blank")
            return normalized
        return value

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value


class NormalizedPortfolioRiskSnapshot(PortfolioRiskModel):
    """One immutable source-issued account/portfolio context snapshot version."""

    source: PortfolioRiskSource
    account: SourceAccountKey
    snapshot_occurrence_id: str = Field(min_length=1, max_length=160)
    account_state: ReportedAccountState
    positions: tuple[ReportedPortfolioPosition, ...] = ()
    risk_limits: tuple[ReportedRiskLimit, ...] = ()
    as_of: datetime
    published_at: datetime | None = None
    snapshot_version: int = Field(ge=1)
    revision_of_snapshot_id: str | None = Field(default=None, max_length=160)
    received_at: datetime

    @field_validator("snapshot_occurrence_id", "revision_of_snapshot_id", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("as_of", "published_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio-risk timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator("positions")
    @classmethod
    def order_positions(
        cls,
        values: tuple[ReportedPortfolioPosition, ...],
    ) -> tuple[ReportedPortfolioPosition, ...]:
        ids = [value.source_position_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("portfolio snapshots cannot contain duplicate source_position_id values")
        return tuple(sorted(values, key=lambda value: value.source_position_id))

    @field_validator("risk_limits")
    @classmethod
    def order_limits(cls, values: tuple[ReportedRiskLimit, ...]) -> tuple[ReportedRiskLimit, ...]:
        ids = [value.source_limit_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("portfolio snapshots cannot contain duplicate source_limit_id values")
        return tuple(sorted(values, key=lambda value: value.source_limit_id))

    @model_validator(mode="after")
    def validate_snapshot(self) -> "NormalizedPortfolioRiskSnapshot":
        if self.source.source_id != self.account.source_id:
            raise ValueError("portfolio-risk source_id must match the account key")
        if self.snapshot_version == 1 and self.revision_of_snapshot_id is not None:
            raise ValueError("version-one portfolio snapshots cannot declare a revision link")
        if self.snapshot_version > 1 and self.revision_of_snapshot_id is None:
            raise ValueError("revised portfolio snapshots require revision_of_snapshot_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        """Semantic snapshot fingerprint; local receipt time is intentionally excluded."""
        return _fingerprint(
            {
                "source": _canonical_source(self.source),
                "account": _canonical_account(self.account),
                "snapshot_occurrence_id": self.snapshot_occurrence_id,
                "account_state": _canonical_account_state(self.account_state),
                "positions": [_canonical_position(position) for position in self.positions],
                "risk_limits": [_canonical_limit(limit) for limit in self.risk_limits],
                "as_of": self.as_of.isoformat(),
                "published_at": self.published_at.isoformat() if self.published_at else None,
                "snapshot_version": self.snapshot_version,
                "revision_of_snapshot_id": self.revision_of_snapshot_id,
            }
        )

    @computed_field(return_type=str)
    @property
    def snapshot_id(self) -> str:
        return "portfolio-risk-snapshot-" + _fingerprint(
            {
                "source_id": self.source.source_id,
                "account_id": self.account.account_id,
                "snapshot_occurrence_id": self.snapshot_occurrence_id,
                "snapshot_version": self.snapshot_version,
                "fingerprint": self.fingerprint,
            }
        )


def _canonical_source(source: PortfolioRiskSource) -> dict[str, str | None]:
    return {
        "source_id": source.source_id,
        "display_name": source.display_name,
        "transport": source.transport.value,
        "domain": source.domain.value,
        "origin_id": source.origin_id,
        "origin_name": source.origin_name,
    }


def _canonical_account(account: SourceAccountKey) -> dict[str, str | None]:
    return {
        "source_id": account.source_id,
        "source_account_id": account.source_account_id,
        "account_reference": account.account_reference,
    }


def _canonical_value(value: EconomicValue | None) -> dict[str, str] | None:
    if value is None:
        return None
    normalized = format(value.value.normalize(), "f")
    if normalized == "-0":
        normalized = "0"
    return {"value": normalized, "unit": value.unit.value, "scale": value.scale.value}


def _canonical_account_state(state: ReportedAccountState) -> dict[str, object]:
    return {
        "currency": state.currency,
        "balance": _canonical_value(state.balance),
        "equity": _canonical_value(state.equity),
        "reference_equity": _canonical_value(state.reference_equity),
        "available_funds": _canonical_value(state.available_funds),
        "margin_used": _canonical_value(state.margin_used),
        "margin_available": _canonical_value(state.margin_available),
        "unrealized_pnl": _canonical_value(state.unrealized_pnl),
        "realized_pnl": _canonical_value(state.realized_pnl),
        "daily_loss": _canonical_value(state.daily_loss),
        "status": state.status.value,
    }


def _canonical_position(position: ReportedPortfolioPosition) -> dict[str, object]:
    return {
        "source_position_id": position.source_position_id,
        "source_instrument_id": position.source_instrument_id,
        "source_symbol": position.source_symbol,
        "venue": position.venue,
        "reported_side": position.reported_side.value,
        "quantity": _canonical_value(position.quantity),
        "price_currency": position.price_currency,
        "average_entry_price": _canonical_value(position.average_entry_price),
        "reference_price": _canonical_value(position.reference_price),
        "realized_pnl": _canonical_value(position.realized_pnl),
        "unrealized_pnl": _canonical_value(position.unrealized_pnl),
        "pnl_currency": position.pnl_currency,
        "status": position.status.value,
        "opened_at": position.opened_at.isoformat() if position.opened_at else None,
    }


def _canonical_limit(limit: ReportedRiskLimit) -> dict[str, object]:
    return {
        "source_limit_id": limit.source_limit_id,
        "kind": limit.kind.value,
        "value": _canonical_value(limit.value),
        "label": limit.label,
    }


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()}"


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
