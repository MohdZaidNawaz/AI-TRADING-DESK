"""Immutable provider-neutral contracts for source-reported corporate actions.

Corporate actions are discrete issuer facts.  They deliberately remain separate
from market prices, financial statements, estimates, and any price-adjustment or
total-return methodology.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.economic_events import EconomicValue, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.schemas import APIModel


class CorporateActionModel(APIModel):
    """Frozen base for corporate-action boundary contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CorporateActionType(str, Enum):
    CASH_DIVIDEND = "cash_dividend"
    STOCK_SPLIT = "stock_split"
    REVERSE_STOCK_SPLIT = "reverse_stock_split"


class CorporateActionLifecycle(str, Enum):
    ANNOUNCED = "announced"
    EFFECTIVE = "effective"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class CorporateActionSource(CorporateActionModel):
    """Immutable connector identity for source-reported corporate actions."""

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
    def validate_source(self) -> "CorporateActionSource":
        if self.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("corporate-action sources must use the fundamental_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("corporate-action source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class CorporateActionInstrumentKey(CorporateActionModel):
    """Source-scoped issuer/instrument identity without cross-provider reconciliation."""

    source_id: str = Field(min_length=1, max_length=80)
    source_instrument_id: str = Field(min_length=1, max_length=160)
    issuer_id: str = Field(min_length=1, max_length=160)
    symbol: str | None = Field(default=None, max_length=40)
    venue: str | None = Field(default=None, max_length=80)

    @field_validator("source_id", "source_instrument_id", "issuer_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("corporate-action instrument identity values cannot be blank")
            return normalized
        return value

    @field_validator("symbol", "venue", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            return normalized or None
        return value

    @computed_field(return_type=str)
    @property
    def instrument_id(self) -> str:
        return _stable_id(
            "corporate-action-instrument",
            self.source_id,
            self.source_instrument_id,
            self.issuer_id,
            self.symbol or "",
            self.venue or "",
        )


class CashDividendTerms(CorporateActionModel):
    """A source-reported cash entitlement, with amount and currency explicit."""

    amount: EconomicValue
    currency: str = Field(min_length=1, max_length=12)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if not normalized:
                raise ValueError("cash-dividend currency cannot be blank")
            return normalized
        return value

    @model_validator(mode="after")
    def validate_amount(self) -> "CashDividendTerms":
        if self.amount.unit is not EconomicValueUnit.CURRENCY:
            raise ValueError("cash-dividend amounts must use the currency unit")
        return self


class ShareAdjustmentTerms(CorporateActionModel):
    """A source-reported share-adjustment ratio without market-value inference."""

    numerator: int = Field(ge=1)
    denominator: int = Field(ge=1)

    @field_validator("numerator", "denominator", mode="before")
    @classmethod
    def require_positive_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("share-adjustment ratio values must be positive whole numbers")
        return value


class NormalizedCorporateAction(CorporateActionModel):
    """One immutable source-issued corporate-action lifecycle version.

    Provider payloads must be fully normalized by a future connector before
    entering this contract; raw provider objects are intentionally absent.
    """

    source: CorporateActionSource
    instrument: CorporateActionInstrumentKey
    occurrence_id: str = Field(min_length=1, max_length=160)
    action_type: CorporateActionType
    lifecycle: CorporateActionLifecycle
    cash_dividend_terms: CashDividendTerms | None = None
    share_adjustment_terms: ShareAdjustmentTerms | None = None
    published_at: datetime
    effective_at: datetime | None = None
    ex_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None
    action_version: int = Field(ge=1)
    revision_of_action_id: str | None = Field(default=None, max_length=160)
    received_at: datetime

    @field_validator("occurrence_id", mode="before")
    @classmethod
    def normalize_occurrence_id(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("corporate-action occurrence_id cannot be blank")
            return normalized
        return value

    @field_validator("revision_of_action_id", mode="before")
    @classmethod
    def normalize_revision_link(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("published_at", "effective_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("corporate-action timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_action(self) -> "NormalizedCorporateAction":
        if self.source.source_id != self.instrument.source_id:
            raise ValueError("corporate-action source_id must match the instrument key")
        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            if self.cash_dividend_terms is None or self.share_adjustment_terms is not None:
                raise ValueError("cash dividends require cash_dividend_terms only")
        elif self.cash_dividend_terms is not None or self.share_adjustment_terms is None:
            raise ValueError("share adjustments require share_adjustment_terms only")
        if self.lifecycle is CorporateActionLifecycle.EFFECTIVE and self.effective_at is None:
            raise ValueError("effective corporate actions require effective_at")
        if self.action_version == 1 and self.revision_of_action_id is not None:
            raise ValueError("version-one corporate actions cannot declare a revision link")
        if self.action_version > 1 and self.revision_of_action_id is None:
            raise ValueError("revised corporate actions require revision_of_action_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        """Semantic content fingerprint; local receipt time intentionally excluded."""
        return _fingerprint(
            {
                "source": _canonical_source(self.source),
                "instrument": _canonical_instrument(self.instrument),
                "occurrence_id": self.occurrence_id,
                "action_type": self.action_type.value,
                "lifecycle": self.lifecycle.value,
                "cash_dividend_terms": _canonical_cash_terms(self.cash_dividend_terms),
                "share_adjustment_terms": _canonical_share_terms(self.share_adjustment_terms),
                "published_at": self.published_at.isoformat(),
                "effective_at": self.effective_at.isoformat() if self.effective_at else None,
                "ex_date": self.ex_date.isoformat() if self.ex_date else None,
                "record_date": self.record_date.isoformat() if self.record_date else None,
                "payment_date": self.payment_date.isoformat() if self.payment_date else None,
                "action_version": self.action_version,
                "revision_of_action_id": self.revision_of_action_id,
            }
        )

    @computed_field(return_type=str)
    @property
    def action_id(self) -> str:
        return "corporate-action-" + _fingerprint(
            {
                "source_id": self.source.source_id,
                "instrument_id": self.instrument.instrument_id,
                "occurrence_id": self.occurrence_id,
                "action_version": self.action_version,
                "fingerprint": self.fingerprint,
            }
        )


def _canonical_source(source: CorporateActionSource) -> dict[str, str | None]:
    return {
        "source_id": source.source_id,
        "display_name": source.display_name,
        "transport": source.transport.value,
        "domain": source.domain.value,
        "origin_id": source.origin_id,
        "origin_name": source.origin_name,
    }


def _canonical_instrument(key: CorporateActionInstrumentKey) -> dict[str, str | None]:
    return {
        "source_id": key.source_id,
        "source_instrument_id": key.source_instrument_id,
        "issuer_id": key.issuer_id,
        "symbol": key.symbol,
        "venue": key.venue,
    }


def _canonical_cash_terms(terms: CashDividendTerms | None) -> dict[str, str] | None:
    if terms is None:
        return None
    amount = format(terms.amount.value.normalize(), "f")
    if Decimal(amount) == 0:
        amount = "0"
    return {
        "amount": amount,
        "unit": terms.amount.unit.value,
        "scale": terms.amount.scale.value,
        "currency": terms.currency,
    }


def _canonical_share_terms(terms: ShareAdjustmentTerms | None) -> dict[str, int] | None:
    if terms is None:
        return None
    return {"numerator": terms.numerator, "denominator": terms.denominator}


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()}"


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
