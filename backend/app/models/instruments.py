"""Provider-neutral instrument catalogue contracts.

Catalogue membership describes provider reference data only.  It never
asserts that a quote is available under the caller's subscription.
"""

from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InstrumentCategory(StrEnum):
    FOREX = "forex"
    COMMODITY = "commodity"
    CRYPTO = "crypto"
    STOCK = "stock"
    ETF = "etf"
    FUTURE = "future"
    INDEX = "index"
    BOND = "bond"


class ExchangeResolution(StrEnum):
    RESOLVED = "resolved"
    VENUE_NEUTRAL = "venue_neutral"
    LEGACY_UNRESOLVED = "legacy_unresolved"


class InstrumentContractKind(StrEnum):
    NONE = "none"
    DATED = "dated"
    CONTINUOUS = "continuous"


def canonical_instrument_id(
    category: InstrumentCategory,
    exchange_id: str,
    canonical_symbol: str,
    *,
    contract_kind: InstrumentContractKind = InstrumentContractKind.NONE,
    contract_expiry: date | None = None,
    continuous_contract_rank: int | None = None,
) -> str:
    """Return a stable identity without treating a bare ticker as globally unique."""
    identity = "\x00".join(
        (
            category.value,
            _normalize_identity_text(exchange_id),
            _normalize_identity_text(canonical_symbol),
            contract_kind.value,
            contract_expiry.isoformat() if contract_expiry is not None else "",
            str(continuous_contract_rank) if continuous_contract_rank is not None else "",
        )
    ).encode("utf-8")
    return f"friday:{category.value}:{sha256(identity).hexdigest()}"


class InstrumentProviderMapping(BaseModel):
    """One provider identifier bound to a canonical instrument and venue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    provider_symbol: str = Field(min_length=1, max_length=120)
    provider_exchange_id: str = Field(min_length=1, max_length=120)
    provider_contract_id: str = Field(default="NONE", min_length=1, max_length=120)
    provider_instrument_id: str | None = Field(default=None, max_length=240)

    @field_validator(
        "source_id", "provider_symbol", "provider_exchange_id", "provider_contract_id"
    )
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("instrument mapping fields cannot be blank")
        return normalized


class InstrumentAlias(BaseModel):
    """An alternate name scoped to a venue and optional source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(min_length=1, max_length=160)
    alias_type: str = Field(default="symbol", min_length=1, max_length=40)
    exchange_id: str = Field(min_length=1, max_length=120)
    source_id: str = Field(default="canonical", min_length=1, max_length=64)


class InstrumentSourceProvenance(BaseModel):
    """Origin and observation time for catalogue membership metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    source_record_id: str = Field(min_length=1, max_length=240)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _require_aware_observation(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provenance timestamps must be timezone-aware")
        return value


class InstrumentCatalogueEntry(BaseModel):
    """A provider-neutral reference record with exact provider identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str = Field(min_length=1, max_length=160)
    canonical_instrument_id: str = Field(min_length=1, max_length=96)
    source_id: str = Field(min_length=1, max_length=64)
    category: InstrumentCategory
    provider_symbol: str = Field(min_length=1, max_length=40)
    canonical_symbol: str = Field(min_length=1, max_length=120)
    exchange_id: str = Field(min_length=1, max_length=120)
    exchange_resolution: ExchangeResolution
    exchange_name: str | None = Field(default=None, max_length=240)
    contract_kind: InstrumentContractKind = InstrumentContractKind.NONE
    contract_expiry: date | None = None
    continuous_contract_rank: int | None = Field(default=None, ge=1, le=99)
    display_symbol: str = Field(min_length=1, max_length=40)
    display_name: str | None = Field(default=None, max_length=240)
    base_currency: str | None = Field(default=None, max_length=120)
    quote_currency: str | None = Field(default=None, max_length=120)
    forex_group: str | None = Field(default=None, max_length=120)
    commodity_category: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1_000)
    available_exchanges: tuple[str, ...] = Field(default_factory=tuple, max_length=200)
    provider_mappings: tuple[InstrumentProviderMapping, ...] = Field(min_length=1)
    aliases: tuple[InstrumentAlias, ...] = ()
    source_provenance: tuple[InstrumentSourceProvenance, ...] = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    quote_access: Literal["unknown"] = "unknown"
    historical_data_access: Literal["unknown"] = "unknown"

    @field_validator("canonical_symbol", "exchange_id")
    @classmethod
    def _normalize_identity_fields(cls, value: str) -> str:
        return _normalize_identity_text(value)

    @model_validator(mode="after")
    def _validate_canonical_identity(self) -> "InstrumentCatalogueEntry":
        expected = canonical_instrument_id(
            self.category,
            self.exchange_id,
            self.canonical_symbol,
            contract_kind=self.contract_kind,
            contract_expiry=self.contract_expiry,
            continuous_contract_rank=self.continuous_contract_rank,
        )
        if self.canonical_instrument_id != expected:
            raise ValueError("canonical instrument identity is inconsistent")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("instrument timestamps must be timezone-aware")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("instrument timestamps must be timezone-aware")
        if self.created_at > self.updated_at:
            raise ValueError("instrument timestamps are inconsistent")
        if self.exchange_resolution is ExchangeResolution.RESOLVED:
            if self.exchange_id == "GLOBAL" or self.exchange_id.startswith(
                "LEGACY_UNRESOLVED:"
            ):
                raise ValueError("resolved exchange identity is inconsistent")
        elif self.exchange_resolution is ExchangeResolution.VENUE_NEUTRAL:
            if self.exchange_id != "GLOBAL":
                raise ValueError("venue-neutral exchange identity is inconsistent")
        elif not self.exchange_id.startswith("LEGACY_UNRESOLVED:"):
            raise ValueError("legacy unresolved exchange identity is inconsistent")
        if self.category is InstrumentCategory.FUTURE:
            if self.contract_kind is InstrumentContractKind.DATED:
                if self.contract_expiry is None or self.continuous_contract_rank is not None:
                    raise ValueError("dated futures contract identity is incomplete")
            elif self.contract_kind is InstrumentContractKind.CONTINUOUS:
                if self.contract_expiry is not None or self.continuous_contract_rank is None:
                    raise ValueError("continuous futures contract identity is incomplete")
            else:
                raise ValueError("futures require a dated or continuous contract identity")
        elif (
            self.contract_kind is not InstrumentContractKind.NONE
            or self.contract_expiry is not None
            or self.continuous_contract_rank is not None
        ):
            raise ValueError("non-futures instruments cannot use futures contract identity")
        if not any(
            mapping.source_id == self.source_id
            and mapping.provider_symbol == self.provider_symbol
            for mapping in self.provider_mappings
        ):
            raise ValueError("primary provider mapping is missing")
        if not any(
            provenance.source_id == self.source_id
            for provenance in self.source_provenance
        ):
            raise ValueError("primary source provenance is missing")
        return self


class InstrumentCataloguePage(BaseModel):
    """One normalized provider catalogue page; no quote entitlement claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    category: InstrumentCategory
    page: int = Field(ge=1)
    outputsize: int | None = Field(default=None, ge=1)
    provider_count: int = Field(ge=0)
    instruments: tuple[InstrumentCatalogueEntry, ...]


class InstrumentCatalogueStatus(BaseModel):
    """Cache state; a stale catalogue remains reference data, not quote data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    is_stale: bool
    instrument_count: int = Field(ge=0)
    refreshed_at: datetime | None = None
    last_refresh_attempt_at: datetime | None = None
    last_refresh_error: str | None = Field(default=None, max_length=120)
    last_refresh_error_category: str | None = Field(default=None, max_length=80)
    last_refresh_error_status_code: int | None = Field(default=None, ge=100, le=599)
    last_refresh_error_provider_code: str | None = Field(default=None, max_length=80)


class InstrumentCatalogueSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=0)
    page: int = Field(ge=1)
    limit: int = Field(ge=1)
    is_stale: bool
    refreshed_at: datetime | None = None
    instruments: tuple[InstrumentCatalogueEntry, ...]


class InstrumentCatalogueInstrumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    is_stale: bool
    refreshed_at: datetime | None = None
    instrument: InstrumentCatalogueEntry


def _normalize_identity_text(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("instrument identity fields cannot be blank")
    return normalized
