"""Strict, non-authorizing contracts for optional LLM narrative synthesis."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


LLMProviderName = Literal["openai", "anthropic", "gemini"]
LLMProviderMode = Literal["auto", "manual"]


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
    fallback_reason: Literal[
        "not_configured",
        "provider_unavailable",
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
    ] | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=300_000)
    analytical_stance: Literal[
        "bullish", "bearish", "neutral", "conflicted", "insufficient_data"
    ] | None = None


class LLMProviderCatalogueEntry(LLMModel):
    provider: LLMProviderName
    configured: bool
    available: bool
    health: Literal["unknown", "healthy", "unavailable"]
    model: str | None = Field(default=None, max_length=120)


class LLMProviderCatalogue(LLMModel):
    configured_mode: LLMProviderMode
    manual_fallback_enabled: bool
    providers: tuple[LLMProviderCatalogueEntry, ...]
