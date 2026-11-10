"""Typed, non-authorizing News and Economic Intelligence response contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from app.models.economic_events import (
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
)
from app.models.schemas import APIModel


class NewsIntelligenceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    FUTURE_DATED = "future_dated"
    UNKNOWN = "unknown"


class NewsRetrievalStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    NOT_CONFIGURED = "not_configured"
    NOT_REQUESTED = "not_requested"
    UNSUPPORTED = "unsupported"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    AUTHENTICATION_ERROR = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    SERVER_ERROR = "server_error"
    PROVIDER_ERROR = "provider_error"
    MALFORMED = "malformed"


class NewsCooldownStatus(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    UNSPECIFIED = "cooldown_unspecified"


class NewsCacheStatus(str, Enum):
    NONE = "none"
    FRESH = "fresh_cache"
    STALE = "stale_cache"


class NewsEvidenceOrigin(str, Enum):
    NEWLY_RETRIEVED = "newly_retrieved"
    CACHED = "cached"


class CalendarProviderStatus(str, Enum):
    EVIDENCE_AVAILABLE = "evidence_available"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class MacroCategory(str, Enum):
    CENTRAL_BANK = "central_bank"
    INFLATION = "inflation"
    EMPLOYMENT = "employment"
    INTEREST_RATES = "interest_rates"
    US_DOLLAR = "us_dollar"
    GEOPOLITICS = "geopolitics"
    ECONOMIC_RELEASE = "economic_release"


class NewsRetrievalDiagnostic(NewsIntelligenceModel):
    status: NewsRetrievalStatus
    provider_id: str = "alpha_vantage_news"
    provider_name: str = "Alpha Vantage News & Sentiment"
    request_count: int = Field(default=0, ge=0, le=10)
    search_queries: tuple[str, ...] = Field(default=(), max_length=10)
    requested_count: int = Field(default=0, ge=0, le=50)
    received_count: int = Field(default=0, ge=0)
    validated_count: int = Field(default=0, ge=0)
    relevant_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    stale_count: int = Field(default=0, ge=0)
    retrieved_at: datetime | None = None
    evidence_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    error_category: Literal[
        "configuration",
        "network",
        "authentication",
        "invalid_request",
        "rate_limit",
        "timeout",
        "server_error",
        "malformed_response",
        "provider_error",
    ] | None = None
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    http_status_class: Literal["1xx", "2xx", "3xx", "4xx", "5xx"] | None = None
    provider_reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Z0-9_]+$",
    )
    failure_stage: Literal[
        "configuration",
        "request",
        "http_response",
        "provider_response",
        "response_decode",
        "response_validation",
        "provider_message",
        "article_normalization",
        "unknown",
    ] | None = None
    request_sent: bool | None = None
    retry_after_seconds: int | None = Field(
        default=None,
        ge=0,
        le=2_147_483_647,
    )
    cooldown_status: NewsCooldownStatus = NewsCooldownStatus.INACTIVE
    cooldown_until: datetime | None = None
    cache_status: NewsCacheStatus = NewsCacheStatus.NONE
    cached_evidence_count: int = Field(default=0, ge=0)
    newly_retrieved_evidence_count: int = Field(default=0, ge=0)
    cached_evidence_ids: tuple[str, ...] = ()
    new_evidence_ids: tuple[str, ...] = ()

    @field_validator("retrieved_at", "cooldown_until")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("diagnostic timestamps must include a timezone")
        return value


class NewsIntelligenceItem(NewsIntelligenceModel):
    evidence_id: str
    provider_id: str
    provider_name: str
    source_name: str
    headline: str
    url: str
    published_at: datetime | None = None
    seen_at: datetime | None = None
    event_at: datetime
    timestamp_basis: Literal["original_publication", "provider_seen", "provider_event"]
    retrieved_at: datetime
    freshness: EvidenceFreshness
    evidence_status: Literal["validated"] = "validated"
    evidence_origin: NewsEvidenceOrigin = NewsEvidenceOrigin.NEWLY_RETRIEVED
    symbols: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    publisher_domain: str | None = None
    language: str | None = None
    source_country: str | None = None
    search_query: str | None = None
    search_category: str | None = None
    relevance_reasons: tuple[str, ...] = ()
    summary: str | None = None
    sentiment_label: str | None = None


class EconomicCalendarItem(NewsIntelligenceModel):
    evidence_id: str
    occurrence_id: str
    event_name: str
    country: str | None = None
    currency: str | None = None
    scheduled_at: datetime
    released_at: datetime | None = None
    status: EconomicReleaseStatus
    impact: EconomicImpact
    previous: EconomicValue | None = None
    forecast: EconomicValue | None = None
    actual: EconomicValue | None = None
    provider_id: str
    provider_name: str
    retrieved_at: datetime
    freshness: EvidenceFreshness
    evidence_status: Literal["validated"] = "validated"


class MacroFinding(NewsIntelligenceModel):
    category: MacroCategory
    verified_fact: str
    interpretation: str
    possible_gold_implication: str
    evidence_ids: tuple[str, ...]
    confidence: Literal["low", "medium"]
    uncertainty: str


class EventRisk(NewsIntelligenceModel):
    evidence_id: str
    event_name: str
    scheduled_at: datetime
    impact: EconomicImpact
    risk_statement: str


class NewsEconomicIntelligenceReport(NewsIntelligenceModel):
    analysis_mode: Literal["deterministic_evidence"] = "deterministic_evidence"
    subject: str
    evaluated_at: datetime
    news_retrieval: NewsRetrievalDiagnostic
    news_provider_statuses: tuple[NewsRetrievalDiagnostic, ...] = ()
    calendar_provider_status: CalendarProviderStatus
    calendar_reason_codes: tuple[str, ...]
    calendar_error_category: Literal["configuration"] | None = None
    relevant_news: tuple[NewsIntelligenceItem, ...] = ()
    upcoming_events: tuple[EconomicCalendarItem, ...] = ()
    released_events: tuple[EconomicCalendarItem, ...] = ()
    macro_findings: tuple[MacroFinding, ...] = ()
    event_risks: tuple[EventRisk, ...] = ()
    missing_information: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False
