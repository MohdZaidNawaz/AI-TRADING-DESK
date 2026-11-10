"""Provider-independent research-request resolution contracts.

These models describe research preparation only. A resolved subject is not a
quote entitlement, provider authentication, trading binding, or authority.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.instruments import ExchangeResolution, InstrumentCategory


class InstrumentResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class InstrumentIdentitySource(StrEnum):
    CATALOGUE = "catalogue"
    STANDARD_REFERENCE = "standard_reference"
    NONE = "none"


class ResearchIntent(StrEnum):
    GENERAL = "general"
    TECHNICAL = "technical"
    MACRO = "macro"
    NEWS = "news"
    LIQUIDITY = "liquidity"
    RISK = "risk"
    COMPARISON = "comparison"


class EvidenceAvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    STALE = "stale"
    PARTIAL = "partial"
    NOT_REQUESTED = "not_requested"


class HistoricalRetrievalStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    CACHE_HIT = "cache_hit"
    RETRIEVED = "retrieved"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    NOT_CONFIGURED = "not_configured"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"


class HistoricalRetrievalDiagnostic(BaseModel):
    """Sanitized retrieval outcome; never contains credentials or raw provider data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: HistoricalRetrievalStatus
    provider_id: Literal["twelve_data"] = "twelve_data"
    provider_symbol: str | None = Field(default=None, max_length=40)
    timeframe: str | None = Field(default=None, max_length=20)
    requested_count: int = Field(default=0, ge=0, le=5_000)
    validated_count: int = Field(default=0, ge=0, le=5_000)
    retrieved_at: datetime | None = None
    missing_interval_count: int = Field(default=0, ge=0)
    provisional_count: int = Field(default=0, ge=0)
    partial: bool = False
    reason_codes: tuple[str, ...] = Field(min_length=1)
    error_category: str | None = Field(default=None, max_length=80)


class ResearchLookback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int = Field(ge=1, le=100_000)
    unit: Literal["bars", "minutes", "hours", "days", "weeks", "months", "years"]


class ResolvedResearchInstrument(BaseModel):
    """Resolution result without inventing provider or venue identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expression: str = Field(min_length=1, max_length=160)
    status: InstrumentResolutionStatus
    identity_source: InstrumentIdentitySource
    instrument_id: str | None = Field(default=None, max_length=160)
    canonical_instrument_id: str | None = Field(default=None, max_length=160)
    canonical_symbol: str | None = Field(default=None, max_length=120)
    provider_source_id: str | None = Field(default=None, max_length=64)
    provider_symbol: str | None = Field(default=None, max_length=120)
    provider_exchange_id: str | None = Field(default=None, max_length=120)
    category: InstrumentCategory | None = None
    display_name: str | None = Field(default=None, max_length=240)
    base_currency: str | None = Field(default=None, max_length=120)
    quote_currency: str | None = Field(default=None, max_length=120)
    exchange_id: str | None = Field(default=None, max_length=120)
    exchange_resolution: ExchangeResolution | None = None
    candidate_instrument_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_resolution(self) -> "ResolvedResearchInstrument":
        if self.status is InstrumentResolutionStatus.RESOLVED:
            if self.canonical_symbol is None or self.category is None:
                raise ValueError("resolved instruments require a canonical subject")
            if self.identity_source is InstrumentIdentitySource.CATALOGUE:
                if self.instrument_id is None or self.canonical_instrument_id is None:
                    raise ValueError("catalogue resolution requires exact identities")
            elif self.identity_source is not InstrumentIdentitySource.STANDARD_REFERENCE:
                raise ValueError("resolved instrument identity source is invalid")
        elif self.identity_source is not InstrumentIdentitySource.NONE:
            raise ValueError("unresolved instruments cannot claim an identity source")
        return self


class EvidenceAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: EvidenceAvailabilityStatus
    record_count: int = Field(default=0, ge=0)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    provider_id: str | None = Field(default=None, max_length=80)
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    last_source_candle_at: datetime | None = None
    retrieved_at: datetime | None = None
    missing_interval_count: int = Field(default=0, ge=0)
    provisional_count: int = Field(default=0, ge=0)
    warning_codes: tuple[str, ...] = ()
    error_category: str | None = Field(default=None, max_length=80)


class ResearchEvidenceAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_identity: EvidenceAvailability
    current_quote: EvidenceAvailability
    historical_ohlcv: EvidenceAvailability
    derived_features: EvidenceAvailability
    news: EvidenceAvailability
    macro: EvidenceAvailability
    positioning_order_flow: EvidenceAvailability


class ResearchRequestContext(BaseModel):
    """Structured, non-authorizing context attached to one analysis route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-request/v1"] = "research-request/v1"
    primary_instrument: ResolvedResearchInstrument
    comparison_instruments: tuple[ResolvedResearchInstrument, ...] = ()
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"] | None = None
    lookback: ResearchLookback | None = None
    intents: tuple[ResearchIntent, ...] = Field(min_length=1)
    analysis_status: Literal["research_preparation_only"] = "research_preparation_only"
    unavailable_capabilities: tuple[str, ...] = Field(min_length=1)
    missing_information: tuple[str, ...] = ()
    evidence: ResearchEvidenceAvailability
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False
