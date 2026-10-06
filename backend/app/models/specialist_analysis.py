"""Immutable, provider-neutral specialist intelligence contracts for FRIDAY.

These internal artifacts retain the distinction between directly observed
normalized evidence and a specialist's interpretation.  They deliberately do
not contain provider payloads, article bodies, trade parameters, credentials,
or execution instructions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.derived_features import (
    DerivedMarketFeature,
    FeatureParameter,
    FeatureQualityFlag,
    FeatureStatus,
    FeatureType,
    FeatureValueUnit,
)
from app.models.events import AssetClass, DataDomain
from app.models.economic_events import (
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
)
from app.models.macro_series import MacroReferencePeriod, MacroSeriesKey
from app.models.positioning import PositioningReferencePeriod, PositioningSeriesKey
from app.models.fundamentals import FundamentalReferencePeriod, FundamentalSeriesKey
from app.models.estimates import EstimateReferencePeriod, EstimateSeriesKey
from app.models.corporate_actions import (
    CashDividendTerms,
    CorporateActionInstrumentKey,
    CorporateActionLifecycle,
    CorporateActionType,
    ShareAdjustmentTerms,
)
from app.models.issuer_reference import (
    InstrumentKind,
    IssuerClassification,
    IssuerInstrumentReferenceKey,
    IssuerReferenceStatus,
)
from app.models.schemas import APIModel


class SpecialistAnalysisModel(APIModel):
    """Frozen internal models used only by the structured intelligence layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SpecialistAnalysisStatus(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    ERROR = "error"


class SpecialistStance(str, Enum):
    """Interpretive stance; intentionally distinct from trade direction."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    CONFLICTED = "conflicted"
    INSUFFICIENT_DATA = "insufficient_data"


class SpecialistOperationalState(str, Enum):
    """A non-authorizing operational state for a specialist capability."""

    DISABLED_NO_EXECUTION = "disabled_no_execution"


class SpecialistConsensus(str, Enum):
    AGREEMENT = "agreement"
    CONFLICTED = "conflicted"
    INSUFFICIENT_DATA = "insufficient_data"


class SpecialistEvidenceReference(SpecialistAnalysisModel):
    """Safe provenance link to normalized evidence, without provider payloads."""

    event_id: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=80)
    domain: DataDomain
    timestamp: datetime
    symbol: str | None = Field(default=None, max_length=40)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp values must include a timezone")
        return value.astimezone(UTC)


class SpecialistFactObservation(SpecialistAnalysisModel):
    """A direct statement about assigned normalized evidence.

    Facts always have at least one provenance reference.  This prevents source
    names, publisher domains, or other metadata from becoming unsupported facts.
    """

    statement: str = Field(min_length=1, max_length=1_000)
    evidence_references: tuple[SpecialistEvidenceReference, ...] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("evidence_references")
    @classmethod
    def order_references(
        cls,
        values: tuple[SpecialistEvidenceReference, ...],
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return _ordered_references(values)


class SpecialistInterpretation(SpecialistAnalysisModel):
    """A specialist interpretation kept explicitly separate from observed facts."""

    statement: str = Field(min_length=1, max_length=1_000)
    evidence_references: tuple[SpecialistEvidenceReference, ...] = Field(
        default=(),
        max_length=100,
    )

    @field_validator("evidence_references")
    @classmethod
    def order_references(
        cls,
        values: tuple[SpecialistEvidenceReference, ...],
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return _ordered_references(values)


class SpecialistDerivedFeatureReference(SpecialistAnalysisModel):
    """Trace a calculated value without representing it as raw provider evidence."""

    feature_id: str = Field(min_length=1, max_length=120)
    feature_type: FeatureType
    status: FeatureStatus
    symbol: str = Field(min_length=1, max_length=40)
    venue: str | None = Field(default=None, max_length=80)
    timeframe: str = Field(min_length=1, max_length=20)
    as_of: datetime | None = None
    value: FiniteFloat | None = None
    value_unit: FeatureValueUnit
    parameters: tuple[FeatureParameter, ...] = ()
    method_id: str = Field(min_length=1, max_length=120)
    method_version: str = Field(min_length=1, max_length=40)
    input_series_id: str = Field(min_length=1, max_length=120)
    input_series_fingerprint: str = Field(min_length=64, max_length=64)
    input_first_bar_id: str | None = Field(default=None, max_length=120)
    input_last_bar_id: str | None = Field(default=None, max_length=120)
    input_bar_count: int = Field(ge=0)
    required_bar_count: int = Field(ge=1)
    quality_flags: tuple[FeatureQualityFlag, ...] = ()
    limitations: tuple[str, ...] = ()

    @classmethod
    def from_feature(
        cls,
        feature: DerivedMarketFeature,
    ) -> "SpecialistDerivedFeatureReference":
        return cls(
            feature_id=feature.feature_id,
            feature_type=feature.feature_type,
            status=feature.status,
            symbol=feature.symbol,
            venue=feature.venue,
            timeframe=feature.timeframe,
            as_of=feature.as_of,
            value=feature.value,
            value_unit=feature.value_unit,
            parameters=feature.parameters,
            method_id=feature.method_id,
            method_version=feature.method_version,
            input_series_id=feature.input_series_id,
            input_series_fingerprint=feature.input_series_fingerprint,
            input_first_bar_id=feature.input_first_bar_id,
            input_last_bar_id=feature.input_last_bar_id,
            input_bar_count=feature.input_bar_count,
            required_bar_count=feature.required_bar_count,
            quality_flags=feature.quality_flags,
            limitations=feature.limitations,
        )

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("derived feature as_of must include a timezone")
        return value.astimezone(UTC)

    @field_validator("parameters")
    @classmethod
    def order_parameters(
        cls,
        values: tuple[FeatureParameter, ...],
    ) -> tuple[FeatureParameter, ...]:
        return tuple(sorted(values, key=lambda item: (item.name, repr(item.value))))

    @field_validator("quality_flags")
    @classmethod
    def order_quality_flags(
        cls,
        values: tuple[FeatureQualityFlag, ...],
    ) -> tuple[FeatureQualityFlag, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))

    @field_validator("limitations")
    @classmethod
    def order_limitations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value.strip() for value in values if value.strip())))

    @model_validator(mode="after")
    def validate_feature_result(self) -> "SpecialistDerivedFeatureReference":
        if self.status is FeatureStatus.AVAILABLE and self.value is None:
            raise ValueError("available derived feature references require a value")
        if self.status is not FeatureStatus.AVAILABLE and self.value is not None:
            raise ValueError("unavailable derived feature references cannot contain a value")
        if self.input_bar_count > 0 and (
            self.input_first_bar_id is None
            or self.input_last_bar_id is None
            or self.as_of is None
        ):
            raise ValueError("derived feature references require complete input lineage")
        return self


class SpecialistDerivedObservation(SpecialistAnalysisModel):
    """A factual calculated observation kept separate from raw facts."""

    statement: str = Field(min_length=1, max_length=1_000)
    feature_references: tuple[SpecialistDerivedFeatureReference, ...] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("feature_references")
    @classmethod
    def order_feature_references(
        cls,
        values: tuple[SpecialistDerivedFeatureReference, ...],
    ) -> tuple[SpecialistDerivedFeatureReference, ...]:
        return _ordered_feature_references(values)


class SpecialistEconomicObservation(SpecialistAnalysisModel):
    """Typed economic evidence with estimates kept separate from observations."""

    event_id: str = Field(min_length=1, max_length=160)
    occurrence_id: str = Field(min_length=1, max_length=160)
    event_name: str = Field(min_length=1, max_length=240)
    indicator_id: str | None = Field(default=None, max_length=160)
    country: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=120)
    currency: str | None = Field(default=None, max_length=12)
    reference_period: str | None = Field(default=None, max_length=120)
    scheduled_at: datetime
    released_at: datetime | None = None
    release_status: EconomicReleaseStatus
    expected_impact: EconomicImpact
    previous: EconomicValue | None = None
    revised_previous: EconomicValue | None = None
    forecast: EconomicValue | None = None
    actual: EconomicValue | None = None
    release_version: int = Field(ge=1)
    revision_of_event_id: str | None = Field(default=None, max_length=160)
    evidence_reference: SpecialistEvidenceReference

    @field_validator("scheduled_at", "released_at")
    @classmethod
    def normalize_economic_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("economic observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_reference(self) -> "SpecialistEconomicObservation":
        if self.evidence_reference.domain is not DataDomain.ECONOMIC_DATA:
            raise ValueError("economic observations require economic_data evidence")
        if self.evidence_reference.event_id != self.event_id:
            raise ValueError("economic observation event identity must match its reference")
        return self


class SpecialistMacroObservation(SpecialistAnalysisModel):
    """Typed raw macro evidence retained by Atlas without a market conclusion."""

    observation_id: str = Field(min_length=1, max_length=160)
    series_id: str = Field(min_length=1, max_length=120)
    series_key: MacroSeriesKey
    reference_period: MacroReferencePeriod
    value: EconomicValue
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    published_at: datetime | None = None
    vintage_at: datetime | None = None
    received_at: datetime
    evidence_reference: SpecialistEvidenceReference

    @field_validator(
        "observed_at",
        "effective_at",
        "published_at",
        "vintage_at",
        "received_at",
    )
    @classmethod
    def normalize_macro_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_macro_reference(self) -> "SpecialistMacroObservation":
        if self.series_id != self.series_key.series_id:
            raise ValueError("macro observation series identity must match its series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("macro observation value unit must match its series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("macro observation value scale must match its series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one macro observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised macro observations require revision_of_observation_id")
        if self.evidence_reference.domain is not DataDomain.ECONOMIC_DATA:
            raise ValueError("macro observations require economic_data evidence")
        if self.evidence_reference.event_id != self.observation_id:
            raise ValueError("macro observation identity must match its evidence reference")
        return self


class SpecialistPositioningObservation(SpecialistAnalysisModel):
    """Typed reported positioning evidence retained by Capitol without inference."""

    observation_id: str = Field(min_length=1, max_length=160)
    series_id: str = Field(min_length=1, max_length=120)
    series_key: PositioningSeriesKey
    reference_period: PositioningReferencePeriod
    value: EconomicValue
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    observed_at: datetime | None = None
    published_at: datetime | None = None
    vintage_at: datetime | None = None
    received_at: datetime
    evidence_reference: SpecialistEvidenceReference

    @field_validator("observed_at", "published_at", "vintage_at", "received_at")
    @classmethod
    def normalize_positioning_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("positioning observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_positioning_reference(self) -> "SpecialistPositioningObservation":
        if self.series_id != self.series_key.series_id:
            raise ValueError("positioning observation series identity must match its series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("positioning observation value unit must match its series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("positioning observation value scale must match its series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one positioning observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised positioning observations require revision_of_observation_id")
        if self.evidence_reference.domain is not DataDomain.POSITIONING_DATA:
            raise ValueError("positioning observations require positioning_data evidence")
        if self.evidence_reference.event_id != self.observation_id:
            raise ValueError("positioning observation identity must match its evidence reference")
        return self


class SpecialistFundamentalObservation(SpecialistAnalysisModel):
    """Typed reported financial evidence retained by Athena without inference."""

    observation_id: str = Field(min_length=1, max_length=160)
    series_id: str = Field(min_length=1, max_length=120)
    series_key: FundamentalSeriesKey
    reference_period: FundamentalReferencePeriod
    value: EconomicValue
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    reported_at: datetime | None = None
    filed_at: datetime | None = None
    published_at: datetime | None = None
    vintage_at: datetime | None = None
    received_at: datetime
    evidence_reference: SpecialistEvidenceReference

    @field_validator(
        "reported_at",
        "filed_at",
        "published_at",
        "vintage_at",
        "received_at",
    )
    @classmethod
    def normalize_fundamental_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fundamental observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_fundamental_reference(self) -> "SpecialistFundamentalObservation":
        if self.series_id != self.series_key.series_id:
            raise ValueError("fundamental observation series identity must match its series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("fundamental observation value unit must match the series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("fundamental observation value scale must match the series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one fundamental observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised fundamental observations require revision_of_observation_id")
        if self.evidence_reference.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("fundamental observations require fundamental_data evidence")
        if self.evidence_reference.event_id != self.observation_id:
            raise ValueError("fundamental observation identity must match its evidence reference")
        return self


class SpecialistEstimateObservation(SpecialistAnalysisModel):
    """Typed source-reported estimate evidence retained by Athena without inference."""

    observation_id: str = Field(min_length=1, max_length=160)
    series_id: str = Field(min_length=1, max_length=120)
    series_key: EstimateSeriesKey
    reference_period: EstimateReferencePeriod
    value: EconomicValue
    as_of: datetime
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    published_at: datetime | None = None
    vintage_at: datetime | None = None
    received_at: datetime
    evidence_reference: SpecialistEvidenceReference

    @field_validator("as_of", "published_at", "vintage_at", "received_at")
    @classmethod
    def normalize_estimate_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("estimate observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_estimate_reference(self) -> "SpecialistEstimateObservation":
        if self.series_id != self.series_key.series_id:
            raise ValueError("estimate observation series identity must match its series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("estimate observation value unit must match the series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("estimate observation value scale must match the series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one estimate observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised estimate observations require revision_of_observation_id")
        if self.evidence_reference.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("estimate observations require fundamental_data evidence")
        if self.evidence_reference.event_id != self.observation_id:
            raise ValueError("estimate observation identity must match its evidence reference")
        return self


class SpecialistCorporateActionObservation(SpecialistAnalysisModel):
    """Typed issuer-action fact without price adjustment or trade inference."""

    action_id: str = Field(min_length=1, max_length=160)
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
    evidence_reference: SpecialistEvidenceReference

    @field_validator("published_at", "effective_at")
    @classmethod
    def normalize_corporate_action_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("corporate-action observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_corporate_action_reference(self) -> "SpecialistCorporateActionObservation":
        if self.evidence_reference.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("corporate actions require fundamental_data evidence")
        if self.evidence_reference.event_id != self.action_id:
            raise ValueError("corporate-action identity must match its evidence reference")
        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            if self.cash_dividend_terms is None or self.share_adjustment_terms is not None:
                raise ValueError("cash-dividend observations require cash terms only")
        elif self.cash_dividend_terms is not None or self.share_adjustment_terms is None:
            raise ValueError("share-adjustment observations require share terms only")
        return self


class SpecialistIssuerReferenceObservation(SpecialistAnalysisModel):
    """Typed source-reported issuer/listing context without valuation inference."""

    profile_id: str = Field(min_length=1, max_length=160)
    instrument: IssuerInstrumentReferenceKey
    source_profile_id: str = Field(min_length=1, max_length=160)
    issuer_display_name: str = Field(min_length=1, max_length=240)
    issuer_legal_name: str | None = Field(default=None, max_length=240)
    instrument_display_name: str | None = Field(default=None, max_length=240)
    asset_class: AssetClass
    instrument_kind: InstrumentKind
    status: IssuerReferenceStatus
    classification: IssuerClassification | None = None
    country: str | None = Field(default=None, max_length=80)
    domicile_country: str | None = Field(default=None, max_length=80)
    reference_currency: str | None = Field(default=None, max_length=12)
    as_of: datetime
    published_at: datetime | None = None
    profile_version: int = Field(ge=1)
    revision_of_profile_id: str | None = Field(default=None, max_length=160)
    evidence_reference: SpecialistEvidenceReference

    @field_validator("as_of", "published_at")
    @classmethod
    def normalize_issuer_reference_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issuer-reference observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_issuer_reference(self) -> "SpecialistIssuerReferenceObservation":
        if self.evidence_reference.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("issuer references require fundamental_data evidence")
        if self.evidence_reference.event_id != self.profile_id:
            raise ValueError("issuer-reference identity must match its evidence reference")
        if self.profile_version == 1 and self.revision_of_profile_id is not None:
            raise ValueError("version-one issuer references cannot declare a revision link")
        if self.profile_version > 1 and self.revision_of_profile_id is None:
            raise ValueError("revised issuer references require revision_of_profile_id")
        return self


class SpecialistLifecycleObservation(SpecialistAnalysisModel):
    """Current-run metadata retained by Ledger without inventing history."""

    trigger_type: Literal["user_query", "market_event", "news_event"]
    trigger_event_id: str | None = Field(default=None, max_length=120)
    prior_agent_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("prior_agent_ids")
    @classmethod
    def order_prior_agents(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(dict.fromkeys(values)))


class SpecialistAnalysis(SpecialistAnalysisModel):
    """One immutable, internal result from a FRIDAY specialist.

    ``stance`` communicates an analytical posture only.  It is never a trade
    instruction and must not be converted to ``TradeDirection`` automatically.
    """

    analysis_id: str = Field(min_length=1, max_length=120)
    analysis_fingerprint: str = Field(min_length=64, max_length=64)
    agent_id: str = Field(min_length=1, max_length=80)
    status: SpecialistAnalysisStatus
    created_at: datetime
    evidence_fingerprint: str = Field(min_length=1, max_length=64)
    confidence: FiniteFloat = Field(ge=0.0, le=1.0)
    stance: SpecialistStance = SpecialistStance.INSUFFICIENT_DATA
    thesis: str = Field(min_length=1, max_length=2_000)
    fact_observations: tuple[SpecialistFactObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    interpretations: tuple[SpecialistInterpretation, ...] = Field(
        default=(),
        max_length=250,
    )
    derived_observations: tuple[SpecialistDerivedObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    economic_observations: tuple[SpecialistEconomicObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    macro_observations: tuple[SpecialistMacroObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    positioning_observations: tuple[SpecialistPositioningObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    fundamental_observations: tuple[SpecialistFundamentalObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    estimate_observations: tuple[SpecialistEstimateObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    corporate_action_observations: tuple[SpecialistCorporateActionObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    issuer_reference_observations: tuple[SpecialistIssuerReferenceObservation, ...] = Field(
        default=(),
        max_length=250,
    )
    supporting_evidence: tuple[SpecialistEvidenceReference, ...] = Field(
        default=(),
        max_length=250,
    )
    derived_feature_references: tuple[SpecialistDerivedFeatureReference, ...] = Field(
        default=(),
        max_length=250,
    )
    invalidating_conditions: tuple[str, ...] = Field(default=(), max_length=100)
    limitations: tuple[str, ...] = Field(default=(), max_length=100)
    timeframes: tuple[str, ...] = Field(default=(), max_length=50)
    lifecycle: SpecialistLifecycleObservation | None = None
    operational_state: SpecialistOperationalState | None = None

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("fact_observations")
    @classmethod
    def order_facts(
        cls,
        values: tuple[SpecialistFactObservation, ...],
    ) -> tuple[SpecialistFactObservation, ...]:
        return tuple(sorted(dict.fromkeys(values), key=_fact_sort_key))

    @field_validator("interpretations")
    @classmethod
    def order_interpretations(
        cls,
        values: tuple[SpecialistInterpretation, ...],
    ) -> tuple[SpecialistInterpretation, ...]:
        return tuple(sorted(dict.fromkeys(values), key=_interpretation_sort_key))

    @field_validator("derived_observations")
    @classmethod
    def order_derived_observations(
        cls,
        values: tuple[SpecialistDerivedObservation, ...],
    ) -> tuple[SpecialistDerivedObservation, ...]:
        return tuple(sorted(dict.fromkeys(values), key=_derived_observation_sort_key))

    @field_validator("economic_observations")
    @classmethod
    def order_economic_observations(
        cls,
        values: tuple[SpecialistEconomicObservation, ...],
    ) -> tuple[SpecialistEconomicObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.scheduled_at,
                    item.evidence_reference.source_id,
                    item.occurrence_id,
                    item.release_version,
                    item.event_id,
                ),
            )
        )

    @field_validator("macro_observations")
    @classmethod
    def order_macro_observations(
        cls,
        values: tuple[SpecialistMacroObservation, ...],
    ) -> tuple[SpecialistMacroObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.series_id,
                    item.reference_period.start_date,
                    item.reference_period.end_date,
                    item.observation_version,
                    item.observation_id,
                ),
            )
        )

    @field_validator("positioning_observations")
    @classmethod
    def order_positioning_observations(
        cls,
        values: tuple[SpecialistPositioningObservation, ...],
    ) -> tuple[SpecialistPositioningObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.series_id,
                    item.reference_period.start_date,
                    item.reference_period.end_date,
                    item.observation_version,
                    item.observation_id,
                ),
            )
        )

    @field_validator("fundamental_observations")
    @classmethod
    def order_fundamental_observations(
        cls,
        values: tuple[SpecialistFundamentalObservation, ...],
    ) -> tuple[SpecialistFundamentalObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.series_id,
                    item.reference_period.start_date,
                    item.reference_period.end_date,
                    item.observation_version,
                    item.observation_id,
                ),
            )
        )

    @field_validator("estimate_observations")
    @classmethod
    def order_estimate_observations(
        cls,
        values: tuple[SpecialistEstimateObservation, ...],
    ) -> tuple[SpecialistEstimateObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.series_id,
                    item.reference_period.start_date,
                    item.reference_period.end_date,
                    item.observation_version,
                    item.observation_id,
                ),
            )
        )

    @field_validator("corporate_action_observations")
    @classmethod
    def order_corporate_action_observations(
        cls,
        values: tuple[SpecialistCorporateActionObservation, ...],
    ) -> tuple[SpecialistCorporateActionObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.effective_at or item.published_at,
                    item.instrument.instrument_id,
                    item.occurrence_id,
                    item.action_version,
                    item.action_id,
                ),
            )
        )

    @field_validator("issuer_reference_observations")
    @classmethod
    def order_issuer_reference_observations(
        cls,
        values: tuple[SpecialistIssuerReferenceObservation, ...],
    ) -> tuple[SpecialistIssuerReferenceObservation, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.instrument.instrument_id,
                    item.source_profile_id,
                    item.as_of,
                    item.profile_version,
                    item.profile_id,
                ),
            )
        )

    @field_validator("supporting_evidence")
    @classmethod
    def order_supporting_evidence(
        cls,
        values: tuple[SpecialistEvidenceReference, ...],
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return _ordered_references(values)

    @field_validator("derived_feature_references")
    @classmethod
    def order_derived_feature_references(
        cls,
        values: tuple[SpecialistDerivedFeatureReference, ...],
    ) -> tuple[SpecialistDerivedFeatureReference, ...]:
        return _ordered_feature_references(values)

    @field_validator("invalidating_conditions", "limitations", "timeframes")
    @classmethod
    def order_text_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = (value.strip() for value in values if value.strip())
        return tuple(sorted(dict.fromkeys(normalized)))

    @model_validator(mode="after")
    def validate_traceability(self) -> "SpecialistAnalysis":
        supporting_keys = {_reference_key(reference) for reference in self.supporting_evidence}
        observation_references = (
            reference
            for observation in (*self.fact_observations, *self.interpretations)
            for reference in observation.evidence_references
        )
        if any(_reference_key(reference) not in supporting_keys for reference in observation_references):
            raise ValueError("observations must reference assigned supporting evidence")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.economic_observations
        ):
            raise ValueError("economic observations must reference assigned supporting evidence")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.macro_observations
        ):
            raise ValueError("macro observations must reference assigned supporting evidence")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.positioning_observations
        ):
            raise ValueError("positioning observations must reference assigned supporting evidence")
        if self.agent_id != "capitol_smart_money" and self.positioning_observations:
            raise ValueError("only Capitol Smart Money may retain positioning observations")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.fundamental_observations
        ):
            raise ValueError("fundamental observations must reference assigned supporting evidence")
        if self.agent_id != "athena_analyst" and self.fundamental_observations:
            raise ValueError("only Athena Analyst may retain fundamental observations")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.estimate_observations
        ):
            raise ValueError("estimate observations must reference assigned supporting evidence")
        if self.agent_id != "athena_analyst" and self.estimate_observations:
            raise ValueError("only Athena Analyst may retain estimate observations")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.corporate_action_observations
        ):
            raise ValueError("corporate-action observations must reference assigned supporting evidence")
        if self.agent_id not in {
            "athena_analyst",
            "scout_recon",
            "chartist_technician",
        } and self.corporate_action_observations:
            raise ValueError("only Athena, Scout, and Chartist may retain corporate-action observations")
        if any(
            observation.action_type
            in {CorporateActionType.CASH_DIVIDEND}
            for observation in self.corporate_action_observations
        ) and self.agent_id == "chartist_technician":
            raise ValueError("Chartist may retain split corporate actions only")
        if any(
            _reference_key(observation.evidence_reference) not in supporting_keys
            for observation in self.issuer_reference_observations
        ):
            raise ValueError("issuer-reference observations must reference assigned supporting evidence")
        if self.agent_id not in {"athena_analyst", "scout_recon"} and self.issuer_reference_observations:
            raise ValueError("only Athena and Scout may retain issuer-reference observations")
        feature_keys = {
            _feature_reference_key(reference)
            for reference in self.derived_feature_references
        }
        derived_references = (
            reference
            for observation in self.derived_observations
            for reference in observation.feature_references
        )
        if any(
            _feature_reference_key(reference) not in feature_keys
            for reference in derived_references
        ):
            raise ValueError("derived observations must reference assigned derived features")
        if self.agent_id == "pilot_execution":
            if self.operational_state is not SpecialistOperationalState.DISABLED_NO_EXECUTION:
                raise ValueError("Pilot Execution must remain DISABLED_NO_EXECUTION")
        elif self.operational_state is not None:
            raise ValueError("operational_state is reserved for Pilot Execution")
        return self


class MasterSpecialistSynthesis(SpecialistAnalysisModel):
    """Internal structured synthesis of immutable specialist artifacts."""

    synthesis_id: str = Field(min_length=1, max_length=120)
    analysis_id: str = Field(min_length=1, max_length=120)
    evidence_fingerprint: str = Field(min_length=1, max_length=64)
    specialist_analyses: tuple[SpecialistAnalysis, ...] = Field(default=(), max_length=20)
    participating_analysis_ids: tuple[str, ...] = Field(default=(), max_length=20)
    supporting_evidence: tuple[SpecialistEvidenceReference, ...] = Field(
        default=(),
        max_length=250,
    )
    derived_feature_references: tuple[SpecialistDerivedFeatureReference, ...] = Field(
        default=(),
        max_length=250,
    )
    consensus: SpecialistConsensus
    conflicting_analysis_ids: tuple[str, ...] = Field(default=(), max_length=20)
    insufficient_data_analysis_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator(
        "specialist_analyses",
        mode="after",
    )
    @classmethod
    def order_specialist_analyses(
        cls,
        values: tuple[SpecialistAnalysis, ...],
    ) -> tuple[SpecialistAnalysis, ...]:
        return tuple(sorted(values, key=lambda item: (item.agent_id, item.analysis_id)))

    @field_validator(
        "participating_analysis_ids",
        "conflicting_analysis_ids",
        "insufficient_data_analysis_ids",
    )
    @classmethod
    def order_analysis_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(dict.fromkeys(values)))

    @field_validator("supporting_evidence")
    @classmethod
    def order_supporting_evidence(
        cls,
        values: tuple[SpecialistEvidenceReference, ...],
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return _ordered_references(values)

    @field_validator("derived_feature_references")
    @classmethod
    def order_derived_feature_references(
        cls,
        values: tuple[SpecialistDerivedFeatureReference, ...],
    ) -> tuple[SpecialistDerivedFeatureReference, ...]:
        return _ordered_feature_references(values)


def create_specialist_analysis(
    *,
    parent_analysis_id: str,
    agent_id: str,
    status: SpecialistAnalysisStatus,
    created_at: datetime,
    evidence_fingerprint: str,
    confidence: float,
    stance: SpecialistStance,
    thesis: str,
    fact_observations: tuple[SpecialistFactObservation, ...] = (),
    interpretations: tuple[SpecialistInterpretation, ...] = (),
    derived_observations: tuple[SpecialistDerivedObservation, ...] = (),
    economic_observations: tuple[SpecialistEconomicObservation, ...] = (),
    macro_observations: tuple[SpecialistMacroObservation, ...] = (),
    positioning_observations: tuple[SpecialistPositioningObservation, ...] = (),
    fundamental_observations: tuple[SpecialistFundamentalObservation, ...] = (),
    estimate_observations: tuple[SpecialistEstimateObservation, ...] = (),
    corporate_action_observations: tuple[SpecialistCorporateActionObservation, ...] = (),
    issuer_reference_observations: tuple[SpecialistIssuerReferenceObservation, ...] = (),
    supporting_evidence: tuple[SpecialistEvidenceReference, ...] = (),
    derived_feature_references: tuple[SpecialistDerivedFeatureReference, ...] = (),
    invalidating_conditions: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    timeframes: tuple[str, ...] = (),
    lifecycle: SpecialistLifecycleObservation | None = None,
    operational_state: SpecialistOperationalState | None = None,
) -> SpecialistAnalysis:
    """Create a deterministic immutable artifact from one fixed evidence snapshot."""
    provisional = SpecialistAnalysis(
        analysis_id="pending",
        analysis_fingerprint="0" * 64,
        agent_id=agent_id,
        status=status,
        created_at=created_at,
        evidence_fingerprint=evidence_fingerprint,
        confidence=confidence,
        stance=stance,
        thesis=thesis,
        fact_observations=fact_observations,
        interpretations=interpretations,
        derived_observations=derived_observations,
        economic_observations=economic_observations,
        macro_observations=macro_observations,
        positioning_observations=positioning_observations,
        fundamental_observations=fundamental_observations,
        estimate_observations=estimate_observations,
        corporate_action_observations=corporate_action_observations,
        issuer_reference_observations=issuer_reference_observations,
        supporting_evidence=supporting_evidence,
        derived_feature_references=derived_feature_references,
        invalidating_conditions=invalidating_conditions,
        limitations=limitations,
        timeframes=timeframes,
        lifecycle=lifecycle,
        operational_state=operational_state,
    )
    analysis_id = "specialist-" + _fingerprint(
        {
            "parent_analysis_id": parent_analysis_id,
            "agent_id": agent_id,
            "evidence_fingerprint": evidence_fingerprint,
        }
    )[:32]
    analysis_fingerprint = _fingerprint(
        provisional.model_dump(
            mode="json",
            exclude={"analysis_id", "analysis_fingerprint"},
        )
    )
    return provisional.model_copy(
        update={
            "analysis_id": analysis_id,
            "analysis_fingerprint": analysis_fingerprint,
        }
    )


def summarize_specialist_analyses(
    *,
    parent_analysis_id: str,
    evidence_fingerprint: str,
    analyses: tuple[SpecialistAnalysis, ...],
) -> MasterSpecialistSynthesis:
    """Aggregate specialist agreement without producing trade direction or parameters."""
    ordered_analyses = tuple(sorted(analyses, key=lambda item: (item.agent_id, item.analysis_id)))
    completed = tuple(
        item for item in ordered_analyses if item.status is SpecialistAnalysisStatus.COMPLETED
    )
    stances = {item.stance for item in completed}
    directional_stances = stances & {SpecialistStance.BULLISH, SpecialistStance.BEARISH}
    if (
        SpecialistStance.CONFLICTED in stances
        or directional_stances == {SpecialistStance.BULLISH, SpecialistStance.BEARISH}
    ):
        consensus = SpecialistConsensus.CONFLICTED
        conflicting = tuple(
            item.analysis_id
            for item in completed
            if item.stance
            in {
                SpecialistStance.BULLISH,
                SpecialistStance.BEARISH,
                SpecialistStance.CONFLICTED,
            }
        )
    elif len(directional_stances) == 1 and sum(
        item.stance in directional_stances for item in completed
    ) >= 2:
        consensus = SpecialistConsensus.AGREEMENT
        conflicting = ()
    else:
        consensus = SpecialistConsensus.INSUFFICIENT_DATA
        conflicting = ()

    insufficient = tuple(
        item.analysis_id
        for item in ordered_analyses
        if item.status is not SpecialistAnalysisStatus.COMPLETED
        or item.stance is SpecialistStance.INSUFFICIENT_DATA
    )
    references = tuple(
        reference
        for item in ordered_analyses
        for reference in item.supporting_evidence
    )
    feature_references = tuple(
        reference
        for item in ordered_analyses
        for reference in item.derived_feature_references
    )
    synthesis_payload = {
        "analysis_id": parent_analysis_id,
        "evidence_fingerprint": evidence_fingerprint,
        "analyses": [item.analysis_fingerprint for item in ordered_analyses],
        "consensus": consensus.value,
    }
    return MasterSpecialistSynthesis(
        synthesis_id="master-" + _fingerprint(synthesis_payload)[:32],
        analysis_id=parent_analysis_id,
        evidence_fingerprint=evidence_fingerprint,
        specialist_analyses=ordered_analyses,
        participating_analysis_ids=tuple(item.analysis_id for item in completed),
        supporting_evidence=references,
        derived_feature_references=feature_references,
        consensus=consensus,
        conflicting_analysis_ids=conflicting,
        insufficient_data_analysis_ids=insufficient,
    )


def _ordered_references(
    values: tuple[SpecialistEvidenceReference, ...],
) -> tuple[SpecialistEvidenceReference, ...]:
    unique = {_reference_key(value): value for value in values}
    return tuple(unique[key] for key in sorted(unique))


def _ordered_feature_references(
    values: tuple[SpecialistDerivedFeatureReference, ...],
) -> tuple[SpecialistDerivedFeatureReference, ...]:
    unique = {_feature_reference_key(value): value for value in values}
    return tuple(unique[key] for key in sorted(unique))


def _reference_key(reference: SpecialistEvidenceReference) -> tuple[str, str, str, str, str]:
    return (
        reference.event_id,
        reference.source_id,
        reference.domain.value,
        reference.timestamp.isoformat(),
        reference.symbol or "",
    )


def _feature_reference_key(
    reference: SpecialistDerivedFeatureReference,
) -> tuple[str, str, str, str]:
    return (
        reference.feature_id,
        reference.method_id,
        reference.method_version,
        reference.input_series_fingerprint,
    )


def _fact_sort_key(
    value: SpecialistFactObservation,
) -> tuple[str, tuple[tuple[str, str, str, str, str], ...]]:
    return value.statement, tuple(_reference_key(item) for item in value.evidence_references)


def _interpretation_sort_key(
    value: SpecialistInterpretation,
) -> tuple[str, tuple[tuple[str, str, str, str, str], ...]]:
    return value.statement, tuple(_reference_key(item) for item in value.evidence_references)


def _derived_observation_sort_key(
    value: SpecialistDerivedObservation,
) -> tuple[str, tuple[tuple[str, str, str, str], ...]]:
    return value.statement, tuple(
        _feature_reference_key(item) for item in value.feature_references
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
