"""Immutable, provider-neutral specialist intelligence contracts for FRIDAY.

These internal artifacts retain the distinction between directly observed
normalized evidence and a specialist's interpretation.  They deliberately do
not contain provider payloads, article bodies, trade parameters, credentials,
or execution instructions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.events import DataDomain
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
    supporting_evidence: tuple[SpecialistEvidenceReference, ...] = Field(
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

    @field_validator("supporting_evidence")
    @classmethod
    def order_supporting_evidence(
        cls,
        values: tuple[SpecialistEvidenceReference, ...],
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return _ordered_references(values)

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
    supporting_evidence: tuple[SpecialistEvidenceReference, ...] = (),
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
        supporting_evidence=supporting_evidence,
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
        consensus=consensus,
        conflicting_analysis_ids=conflicting,
        insufficient_data_analysis_ids=insufficient,
    )


def _ordered_references(
    values: tuple[SpecialistEvidenceReference, ...],
) -> tuple[SpecialistEvidenceReference, ...]:
    unique = {_reference_key(value): value for value in values}
    return tuple(unique[key] for key in sorted(unique))


def _reference_key(reference: SpecialistEvidenceReference) -> tuple[str, str, str, str, str]:
    return (
        reference.event_id,
        reference.source_id,
        reference.domain.value,
        reference.timestamp.isoformat(),
        reference.symbol or "",
    )


def _fact_sort_key(
    value: SpecialistFactObservation,
) -> tuple[str, tuple[tuple[str, str, str, str, str], ...]]:
    return value.statement, tuple(_reference_key(item) for item in value.evidence_references)


def _interpretation_sort_key(
    value: SpecialistInterpretation,
) -> tuple[str, tuple[tuple[str, str, str, str, str], ...]]:
    return value.statement, tuple(_reference_key(item) for item in value.evidence_references)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
