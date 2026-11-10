"""Provider-neutral policy and result contracts for Macro Intelligence.

These models define evidence requirements and deterministic result envelopes.
They do not retrieve provider data, calculate macro signals, or authorize a
trade.  Existing economic-event and macro-series models remain the source of
truth for observations and release lifecycles.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.macro_series import (
    MacroFrequency,
    MacroReferencePeriodKind,
    SeasonalAdjustment,
)
from app.models.schemas import APIModel


class MacroIntelligenceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MacroIndicatorCategory(str, Enum):
    INFLATION = "inflation"
    EMPLOYMENT = "employment"
    GROWTH = "growth"
    MONETARY_POLICY = "monetary_policy"
    NOMINAL_YIELD = "nominal_yield"
    REAL_YIELD = "real_yield"
    US_DOLLAR = "us_dollar"


class MacroSourceType(str, Enum):
    OFFICIAL_OBSERVATION = "official_observation"
    COMMERCIAL_OBSERVATION = "commercial_observation"
    CENTRAL_BANK = "central_bank"
    ECONOMIC_CALENDAR = "economic_calendar"
    COMMERCIAL_CONSENSUS = "commercial_consensus"
    MARKET_PRICE_DERIVED = "market_price_derived"


class MacroValueRole(str, Enum):
    ACTUAL = "actual"
    FORECAST = "forecast"
    PREVIOUS = "previous"
    REVISED_PREVIOUS = "revised_previous"
    OBSERVED = "observed"
    EFFECTIVE = "effective"


class MacroFreshnessStatus(str, Enum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"
    NOT_AVAILABLE = "not_available"


class MacroFreshnessBasis(str, Enum):
    PUBLISHED_AT = "published_at"
    PUBLISHED_OR_OBSERVED_AT = "published_or_observed_at"
    EFFECTIVE_AT = "effective_at"
    RELEASED_AT = "released_at"
    SCHEDULED_AND_RETRIEVED_AT = "scheduled_and_retrieved_at"


class MacroFreshnessPolicyId(str, Enum):
    DAILY_MARKET_CONTEXT = "daily_market_context_v1"
    MONTHLY_OBSERVATION = "monthly_observation_v1"
    QUARTERLY_OBSERVATION = "quarterly_observation_v1"
    POLICY_RATE = "policy_rate_v1"
    SCHEDULED_EVENT = "scheduled_event_v1"
    RELEASED_EVENT = "released_event_v1"


class MacroAssessmentStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_INPUT = "invalid_input"
    INCOMPATIBLE = "incompatible"
    STALE = "stale"
    NOT_AVAILABLE = "not_available"


class MacroDirection(str, Enum):
    ABOVE = "above"
    BELOW = "below"
    IN_LINE = "in_line"
    RISING = "rising"
    FALLING = "falling"
    UNCHANGED = "unchanged"


class MacroProviderStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    NOT_CONFIGURED = "not_configured"
    NOT_REQUESTED = "not_requested"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    MALFORMED = "malformed"


class InstrumentMacroClass(str, Enum):
    GOLD = "gold"
    SILVER = "silver"
    FOREX = "forex"


class MacroFactor(str, Enum):
    INFLATION = "inflation"
    POLICY_RATES = "policy_rates"
    REAL_YIELDS = "real_yields"
    NOMINAL_YIELDS = "nominal_yields"
    US_DOLLAR = "us_dollar"
    RISK_CONTEXT = "risk_context"
    INDUSTRIAL_GROWTH = "industrial_growth"
    BASE_CURRENCY_POLICY = "base_currency_policy"
    QUOTE_CURRENCY_POLICY = "quote_currency_policy"
    RATE_DIFFERENTIAL = "rate_differential"
    CURRENCY_SPECIFIC_RELEASES = "currency_specific_releases"


class MacroIndicatorDefinition(MacroIntelligenceModel):
    indicator_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=160)
    category: MacroIndicatorCategory
    country: str | None = Field(default=None, min_length=2, max_length=8)
    region: str | None = Field(default=None, min_length=2, max_length=80)
    currency: str | None = Field(default=None, min_length=3, max_length=12)
    maturity: str | None = Field(default=None, min_length=2, max_length=12)
    expected_frequency: MacroFrequency
    value_unit: EconomicValueUnit
    value_scale: EconomicValueScale
    seasonal_adjustment: SeasonalAdjustment
    reference_period_kind: MacroReferencePeriodKind
    expected_source_type: MacroSourceType
    freshness_policy_id: MacroFreshnessPolicyId

    @field_validator("country", "currency", "maturity")
    @classmethod
    def uppercase_codes(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def require_geography(self) -> "MacroIndicatorDefinition":
        if self.country is None and self.region is None:
            raise ValueError("macro indicator definitions require country or region")
        return self


class MacroFreshnessPolicy(MacroIntelligenceModel):
    policy_id: MacroFreshnessPolicyId
    basis: MacroFreshnessBasis
    fresh_for: timedelta
    aging_for: timedelta
    description: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_windows(self) -> "MacroFreshnessPolicy":
        if self.fresh_for <= timedelta(0):
            raise ValueError("freshness fresh_for must be positive")
        if self.aging_for <= self.fresh_for:
            raise ValueError("freshness aging_for must be greater than fresh_for")
        return self


class MacroSourcePolicy(MacroIntelligenceModel):
    source_type: MacroSourceType
    allowed_value_roles: tuple[MacroValueRole, ...]
    requires_provider_identity: Literal[True] = True
    requires_provenance: Literal[True] = True
    may_establish_consensus: bool = False
    description: str = Field(min_length=1, max_length=300)

    @field_validator("allowed_value_roles")
    @classmethod
    def canonicalize_roles(
        cls,
        values: tuple[MacroValueRole, ...],
    ) -> tuple[MacroValueRole, ...]:
        if not values:
            raise ValueError("source policies require at least one value role")
        return tuple(sorted(set(values), key=lambda item: item.value))


class InstrumentMacroPolicy(MacroIntelligenceModel):
    instrument_class: InstrumentMacroClass
    required_factors: tuple[MacroFactor, ...]
    description: str = Field(min_length=1, max_length=300)

    @field_validator("required_factors")
    @classmethod
    def canonicalize_factors(cls, values: tuple[MacroFactor, ...]) -> tuple[MacroFactor, ...]:
        if not values:
            raise ValueError("instrument macro policies require at least one factor")
        return tuple(sorted(set(values), key=lambda item: item.value))


class MacroEvidenceAvailability(MacroIntelligenceModel):
    """Chronology needed to decide when evidence could first be used."""

    reference_period_start: date
    reference_period_end: date
    published_at: datetime | None = None
    effective_at: datetime | None = None
    retrieved_at: datetime
    vintage_at: datetime | None = None
    required_bases: tuple[Literal["published_at", "effective_at", "vintage_at"], ...]

    @field_validator("published_at", "effective_at", "retrieved_at", "vintage_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro availability timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator("required_bases")
    @classmethod
    def canonicalize_bases(
        cls,
        values: tuple[Literal["published_at", "effective_at", "vintage_at"], ...],
    ) -> tuple[Literal["published_at", "effective_at", "vintage_at"], ...]:
        if not values:
            raise ValueError("at least one availability basis is required")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def validate_chronology(self) -> "MacroEvidenceAvailability":
        if self.reference_period_end < self.reference_period_start:
            raise ValueError("reference period end cannot precede its start")
        for field_name in self.required_bases:
            if getattr(self, field_name) is None:
                raise ValueError(f"required availability timestamp is missing: {field_name}")
        if self.published_at is not None and self.published_at > self.retrieved_at:
            raise ValueError("publication time cannot follow retrieval time")
        if self.vintage_at is not None and self.vintage_at > self.retrieved_at:
            raise ValueError("vintage time cannot follow retrieval time")
        if (
            self.published_at is not None
            and self.vintage_at is not None
            and self.vintage_at < self.published_at
        ):
            raise ValueError("vintage time cannot precede publication time")
        return self

    @computed_field(return_type=datetime)
    @property
    def first_available_at(self) -> datetime:
        values = [getattr(self, field_name) for field_name in self.required_bases]
        return max(value for value in values if value is not None)

    def available_as_of(self, evaluated_at: datetime) -> bool:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("evaluation time must include a timezone")
        return self.first_available_at <= evaluated_at.astimezone(UTC)


class MacroAssessmentResult(MacroIntelligenceModel):
    status: MacroAssessmentStatus
    method_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_.-]+$")
    method_version: str = Field(min_length=1, max_length=40)
    supporting_evidence_ids: tuple[str, ...] = ()
    evidence_freshness: tuple[MacroFreshnessStatus, ...] = ()
    calculated_at: datetime
    missing_inputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("calculated_at")
    @classmethod
    def normalize_calculated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("calculation time must include a timezone")
        return value.astimezone(UTC)

    @field_validator(
        "supporting_evidence_ids",
        "missing_inputs",
        "warnings",
        "limitations",
    )
    @classmethod
    def canonicalize_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values if value.strip())
        if len(normalized) != len(values):
            raise ValueError("assessment lists cannot contain blank values")
        return tuple(sorted(set(normalized)))

    @model_validator(mode="after")
    def validate_status_contract(self) -> "MacroAssessmentResult":
        if self.status is MacroAssessmentStatus.AVAILABLE:
            if not self.supporting_evidence_ids:
                raise ValueError("available assessments require supporting evidence")
            if self.missing_inputs:
                raise ValueError("available assessments cannot declare missing inputs")
            if self.confidence <= 0:
                raise ValueError("available assessments require positive confidence")
            if not self.evidence_freshness:
                raise ValueError("available assessments require evidence freshness")
            if any(
                status
                in {
                    MacroFreshnessStatus.STALE,
                    MacroFreshnessStatus.UNKNOWN,
                    MacroFreshnessStatus.NOT_AVAILABLE,
                }
                for status in self.evidence_freshness
            ):
                raise ValueError("available assessments require usable evidence freshness")
        elif self.status in {
            MacroAssessmentStatus.INSUFFICIENT_DATA,
            MacroAssessmentStatus.NOT_AVAILABLE,
        } and not self.missing_inputs:
            raise ValueError("unavailable assessments require missing inputs")
        return self


class EconomicReleaseSurprise(MacroAssessmentResult):
    event_id: str | None = Field(default=None, max_length=160)
    indicator_id: str | None = Field(default=None, max_length=120)
    actual: EconomicValue | None = None
    forecast: EconomicValue | None = None
    surprise: EconomicValue | None = None
    direction: MacroDirection | None = None
    released_at: datetime | None = None
    retrieval_provider_id: str | None = Field(default=None, max_length=80)
    original_publisher_id: str | None = Field(default=None, max_length=80)

    @field_validator("released_at")
    @classmethod
    def normalize_release_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("release timestamp requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_surprise_contract(self) -> "EconomicReleaseSurprise":
        _validate_derived_value(
            self.status,
            self.surprise,
            (self.actual, self.forecast),
            "release surprise",
        )
        if self.status is not MacroAssessmentStatus.AVAILABLE and self.direction is not None:
            raise ValueError("unavailable release surprise cannot contain direction")
        return self


class EconomicReleaseRevision(MacroAssessmentResult):
    event_id: str | None = Field(default=None, max_length=160)
    indicator_id: str | None = Field(default=None, max_length=120)
    previous: EconomicValue | None = None
    revised_previous: EconomicValue | None = None
    revision: EconomicValue | None = None
    direction: MacroDirection | None = None
    released_at: datetime | None = None

    @field_validator("released_at")
    @classmethod
    def normalize_revision_release_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("revision release timestamp requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_revision_contract(self) -> "EconomicReleaseRevision":
        _validate_derived_value(
            self.status,
            self.revision,
            (self.previous, self.revised_previous),
            "release revision",
        )
        if self.status is not MacroAssessmentStatus.AVAILABLE and self.direction is not None:
            raise ValueError("unavailable release revision cannot contain direction")
        return self


class MacroIndicatorChange(MacroAssessmentResult):
    indicator_id: str = Field(min_length=1, max_length=120)
    current: EconomicValue | None = None
    previous: EconomicValue | None = None
    change: EconomicValue | None = None
    direction: MacroDirection | None = None
    previous_period_end: date | None = None
    current_period_end: date | None = None

    @model_validator(mode="after")
    def validate_change_contract(self) -> "MacroIndicatorChange":
        _validate_derived_value(
            self.status,
            self.change,
            (self.current, self.previous),
            "indicator change",
        )
        if self.status is not MacroAssessmentStatus.AVAILABLE and self.direction is not None:
            raise ValueError("unavailable indicator change cannot contain direction")
        if (
            self.previous_period_end is not None
            and self.current_period_end is not None
            and self.current_period_end <= self.previous_period_end
        ):
            raise ValueError("indicator change periods must advance chronologically")
        return self


class PolicyRateDifferential(MacroAssessmentResult):
    base_currency: str = Field(min_length=3, max_length=12)
    quote_currency: str = Field(min_length=3, max_length=12)
    base_rate: EconomicValue | None = None
    quote_rate: EconomicValue | None = None
    differential: EconomicValue | None = None
    base_effective_at: datetime | None = None
    quote_effective_at: datetime | None = None

    @field_validator("base_currency", "quote_currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("base_effective_at", "quote_effective_at")
    @classmethod
    def normalize_effective_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("policy effective timestamp requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_differential_contract(self) -> "PolicyRateDifferential":
        if self.base_currency == self.quote_currency:
            raise ValueError("policy-rate differential requires two currencies")
        _validate_derived_value(
            self.status,
            self.differential,
            (self.base_rate, self.quote_rate),
            "policy-rate differential",
        )
        return self


class YieldCurveSpread(MacroAssessmentResult):
    short_maturity: str = Field(min_length=2, max_length=12)
    long_maturity: str = Field(min_length=2, max_length=12)
    short_yield: EconomicValue | None = None
    long_yield: EconomicValue | None = None
    spread: EconomicValue | None = None
    short_observed_at: date | None = None
    long_observed_at: date | None = None

    @model_validator(mode="after")
    def validate_spread_contract(self) -> "YieldCurveSpread":
        if self.short_maturity.upper() == self.long_maturity.upper():
            raise ValueError("yield-curve spread requires two maturities")
        _validate_derived_value(
            self.status,
            self.spread,
            (self.short_yield, self.long_yield),
            "yield-curve spread",
        )
        return self


class RealYieldContext(MacroAssessmentResult):
    maturity: str = Field(min_length=2, max_length=12)
    real_yield: EconomicValue | None = None
    nominal_yield: EconomicValue | None = None
    inflation_measure: EconomicValue | None = None

    @model_validator(mode="after")
    def validate_real_yield_contract(self) -> "RealYieldContext":
        if self.status is MacroAssessmentStatus.AVAILABLE and self.real_yield is None:
            raise ValueError("available real-yield context requires a real yield")
        return self


class CrossMarketObservation(MacroAssessmentResult):
    primary_instrument_id: str = Field(min_length=1, max_length=160)
    context_instrument_id: str = Field(min_length=1, max_length=160)
    observation: str | None = Field(default=None, max_length=500)


class InstrumentSpecificMacroAssessment(MacroAssessmentResult):
    instrument_id: str = Field(min_length=1, max_length=160)
    instrument_class: InstrumentMacroClass
    assessed_factors: tuple[MacroFactor, ...] = ()
    verified_facts: tuple[str, ...] = ()
    interpretations: tuple[str, ...] = ()
    conditional_implications: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()

    @field_validator("assessed_factors")
    @classmethod
    def canonicalize_assessed_factors(
        cls,
        values: tuple[MacroFactor, ...],
    ) -> tuple[MacroFactor, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))


class MacroProviderDiagnostic(MacroIntelligenceModel):
    provider_id: str | None = Field(default=None, min_length=1, max_length=120)
    source_type: MacroSourceType
    status: MacroProviderStatus
    requested_at: datetime | None = None
    retrieved_at: datetime | None = None
    request_count: int = Field(default=0, ge=0, le=100)
    received_count: int = Field(default=0, ge=0)
    validated_count: int = Field(default=0, ge=0)
    reason_codes: tuple[str, ...] = ()
    error_category: str | None = Field(default=None, max_length=80)
    failure_stage: str | None = Field(default=None, max_length=80)
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    request_sent: bool | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)
    cached_evidence_count: int = Field(default=0, ge=0)
    new_evidence_count: int = Field(default=0, ge=0)

    @field_validator("requested_at", "retrieved_at")
    @classmethod
    def normalize_diagnostic_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider diagnostic timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_diagnostic(self) -> "MacroProviderDiagnostic":
        if self.provider_id is None and self.status not in {
            MacroProviderStatus.NOT_CONFIGURED,
            MacroProviderStatus.NOT_REQUESTED,
        }:
            raise ValueError("an attempted provider diagnostic requires provider identity")
        if self.retrieved_at is not None and self.requested_at is None:
            raise ValueError("retrieval time requires request time")
        if (
            self.requested_at is not None
            and self.retrieved_at is not None
            and self.retrieved_at < self.requested_at
        ):
            raise ValueError("retrieval time cannot precede request time")
        if self.validated_count > self.received_count:
            raise ValueError("validated provider records cannot exceed received records")
        return self


class MacroObservationSummary(MacroIntelligenceModel):
    observation_id: str = Field(min_length=1, max_length=160)
    indicator_id: str = Field(min_length=1, max_length=120)
    value: EconomicValue
    reference_period_start: date
    reference_period_end: date
    freshness: MacroFreshnessStatus
    published_at: datetime | None = None
    effective_at: datetime | None = None
    vintage_at: datetime | None = None
    retrieved_at: datetime
    retrieval_provider_id: str = Field(min_length=1, max_length=80)
    retrieval_provider_name: str = Field(min_length=1, max_length=120)
    original_publisher_id: str | None = Field(default=None, max_length=80)
    original_publisher_name: str | None = Field(default=None, max_length=120)

    @field_validator("published_at", "effective_at", "vintage_at", "retrieved_at")
    @classmethod
    def normalize_observation_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro observation summary timestamps require a timezone")
        return value.astimezone(UTC)


class MacroIntelligenceReport(MacroIntelligenceModel):
    analysis_mode: Literal["deterministic_macro_contract"] = "deterministic_macro_contract"
    subject: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    provider_diagnostics: tuple[MacroProviderDiagnostic, ...] = ()
    observations: tuple[MacroObservationSummary, ...] = ()
    release_surprises: tuple[EconomicReleaseSurprise, ...] = ()
    release_revisions: tuple[EconomicReleaseRevision, ...] = ()
    indicator_changes: tuple[MacroIndicatorChange, ...] = ()
    policy_rate_differentials: tuple[PolicyRateDifferential, ...] = ()
    yield_curve_spreads: tuple[YieldCurveSpread, ...] = ()
    real_yield_contexts: tuple[RealYieldContext, ...] = ()
    cross_market_observations: tuple[CrossMarketObservation, ...] = ()
    instrument_assessments: tuple[InstrumentSpecificMacroAssessment, ...] = ()
    missing_information: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_evaluated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro report evaluation time must include a timezone")
        return value.astimezone(UTC)


def _validate_derived_value(
    status: MacroAssessmentStatus,
    result: EconomicValue | None,
    inputs: tuple[EconomicValue | None, ...],
    label: str,
) -> None:
    present = tuple(value for value in inputs if value is not None)
    if status is MacroAssessmentStatus.INCOMPATIBLE:
        if result is not None:
            raise ValueError(f"incompatible {label} cannot contain a derived result")
        return
    if present and len({(value.unit, value.scale) for value in present}) > 1:
        raise ValueError(f"{label} inputs must use compatible units and scales")
    if status is MacroAssessmentStatus.AVAILABLE:
        if len(present) != len(inputs) or result is None:
            raise ValueError(f"available {label} requires all inputs and a result")
        if (result.unit, result.scale) != (present[0].unit, present[0].scale):
            raise ValueError(f"{label} result must preserve input unit and scale")
    elif result is not None:
        raise ValueError(f"unavailable {label} cannot contain a derived result")
