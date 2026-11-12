"""Strict, non-authorizing contracts for optional LLM narrative synthesis."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


LLMProviderName = Literal["openai", "anthropic", "gemini", "groq"]
LLMProviderMode = Literal["auto", "manual"]
LLMFallbackReason = Literal[
    "not_configured",
    "provider_unavailable",
    "authentication",
    "timeout",
    "rate_limit",
    "transport_error",
    "invalid_request",
    "malformed_response",
    "schema_validation",
    "evidence_validation",
    "unsafe_output",
    "context_limit",
    "manual_provider_unavailable",
    "budget_exhausted",
    "paid_provider_not_authorized",
    "pricing_unavailable",
    "budget_ledger_unavailable",
]


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LLMNarrativePoint(LLMModel):
    text: str = Field(min_length=1, max_length=500)
    evidence_reference_ids: tuple[str, ...] = Field(default=(), max_length=12)


class ChiefStrategistNarrative(LLMModel):
    executive_summary: str = Field(min_length=1, max_length=1200)
    analytical_stance: Literal[
        "bullish", "bearish", "neutral", "conflicted", "insufficient_data"
    ]
    supporting_points: tuple[LLMNarrativePoint, ...] = Field(max_length=8)
    counterpoints: tuple[LLMNarrativePoint, ...] = Field(max_length=8)
    conditional_scenarios: tuple[LLMNarrativePoint, ...] = Field(max_length=6)
    invalidation_conditions: tuple[LLMNarrativePoint, ...] = Field(max_length=6)
    missing_evidence: tuple[str, ...] = Field(max_length=12)
    limitations: tuple[str, ...] = Field(max_length=12)
    evidence_reference_ids: tuple[str, ...] = Field(max_length=24)

    @field_validator("missing_evidence", "limitations")
    @classmethod
    def bounded_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 400 for item in value):
            raise ValueError("narrative list items must be non-empty and bounded")
        return value


class LLMSynthesisMetadata(LLMModel):
    mode: LLMProviderMode
    status: Literal["llm_synthesis", "deterministic_fallback"]
    provider: LLMProviderName | None = None
    model: str | None = Field(default=None, max_length=120)
    fallback_reason: LLMFallbackReason | None = None
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    provider_error_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
    )
    provider_error_message: str | None = Field(default=None, max_length=240)
    retry_after_seconds: float | None = Field(default=None, ge=0, le=3600)
    latency_ms: int | None = Field(default=None, ge=0, le=300_000)
    analytical_stance: Literal[
        "bullish", "bearish", "neutral", "conflicted", "insufficient_data"
    ] | None = None


class LLMProviderCatalogueEntry(LLMModel):
    provider: LLMProviderName
    configured: bool
    available: bool
    health: Literal[
        "unknown",
        "healthy",
        "temporarily_unavailable",
        "authentication_failed",
        "configuration_error",
    ]
    last_failure_reason: LLMFallbackReason | None = None
    model: str | None = Field(default=None, max_length=120)
    pricing_status: Literal["known", "unknown"]
    tier: Literal["free", "paid"] | None = None
    eligible: bool
    paid_authorization_required: bool
    free_tier_quota_status: Literal["not_applicable", "unverified"]


class LLMProviderCatalogue(LLMModel):
    configured_mode: LLMProviderMode
    manual_fallback_enabled: bool
    providers: tuple[LLMProviderCatalogueEntry, ...]


class LLMBudgetStatus(LLMModel):
    monthly_budget_inr: Decimal = Field(ge=0)
    usable_budget_inr: Decimal = Field(ge=0)
    protected_reserve_inr: Decimal = Field(ge=0)
    daily_limit_inr: Decimal = Field(ge=0)
    maximum_request_estimate_inr: Decimal = Field(ge=0)
    paid_provider_authorized: bool
    monthly_committed_inr: Decimal = Field(ge=0)
    daily_committed_inr: Decimal = Field(ge=0)
    monthly_spent_inr: Decimal = Field(ge=0)
    daily_spent_inr: Decimal = Field(ge=0)
    pending_reserved_inr: Decimal = Field(ge=0)
    remaining_usable_inr: Decimal = Field(ge=0)
    pending_request_count: int = Field(ge=0)
    provider_pricing: tuple["LLMProviderPricingStatus", ...] = ()


class LLMProviderPricingStatus(LLMModel):
    provider: LLMProviderName
    model: str | None = Field(default=None, max_length=120)
    configured: bool
    pricing_status: Literal["known", "unknown"]
    tier: Literal["free", "paid"] | None = None
    health: Literal[
        "unknown",
        "healthy",
        "temporarily_unavailable",
        "authentication_failed",
        "configuration_error",
    ]
    eligible: bool
    paid_authorization_required: bool
    free_tier_quota_status: Literal["not_applicable", "unverified"]


class LLMUsageRecord(LLMModel):
    request_id: str = Field(min_length=36, max_length=36)
    provider: LLMProviderName
    model: str = Field(min_length=1, max_length=120)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_inr: Decimal = Field(ge=0)
    actual_cost_inr: Decimal | None = Field(default=None, ge=0)
    state: Literal["reserved", "completed", "released"]
    timestamp: datetime
