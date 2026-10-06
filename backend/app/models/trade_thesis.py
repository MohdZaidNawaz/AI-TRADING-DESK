"""Immutable, non-authorizing Master trade-thesis contracts for FRIDAY."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator

from app.models.events import DataDomain
from app.models.schemas import APIModel
from app.models.specialist_analysis import (
    SpecialistAnalysisStatus,
    SpecialistEvidenceReference,
    SpecialistStance,
)


class TradeThesisModel(APIModel):
    """Frozen internal thesis contracts with no execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ThesisAnalyticalBias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    CONFLICTED = "conflicted"
    INSUFFICIENT_DATA = "insufficient_data"


class ThesisConsensusState(str, Enum):
    AGREEMENT = "agreement"
    CONFLICTED = "conflicted"
    INSUFFICIENT_DATA = "insufficient_data"


class ThesisAuthorizationStatus(str, Enum):
    NON_EXECUTABLE_NON_AUTHORIZING = "non_executable_non_authorizing"


class MasterTradeThesisReference(TradeThesisModel):
    """Compact thesis lineage retained by a non-executable proposal."""

    thesis_id: str = Field(min_length=1, max_length=160)
    fingerprint: str = Field(min_length=64, max_length=64)


class ThesisSpecialistArtifactReference(TradeThesisModel):
    """Safe specialist-artifact link without copying evidence or provider payloads."""

    agent_id: str = Field(min_length=1, max_length=80)
    analysis_id: str = Field(min_length=1, max_length=120)
    analysis_fingerprint: str = Field(min_length=64, max_length=64)
    status: SpecialistAnalysisStatus
    stance: SpecialistStance


class ThesisEvidenceCoverage(TradeThesisModel):
    """Factual coverage metadata, intentionally not a quality or risk score."""

    referenced_evidence_count: int = Field(ge=0)
    evidence_domains: tuple[DataDomain, ...] = ()
    specialist_artifact_count: int = Field(ge=0)
    completed_specialist_count: int = Field(ge=0)
    insufficient_specialist_count: int = Field(ge=0)

    @field_validator("evidence_domains")
    @classmethod
    def canonicalize_domains(cls, values: tuple[DataDomain, ...]) -> tuple[DataDomain, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))


class MasterTradeThesis(TradeThesisModel):
    """One deterministic analytical Master artifact; never a trade instruction."""

    thesis_id: str = Field(min_length=1, max_length=160)
    analysis_id: str = Field(min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=40)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    evidence_references: tuple[SpecialistEvidenceReference, ...] = ()
    master_synthesis_id: str = Field(min_length=1, max_length=120)
    master_synthesis_fingerprint: str = Field(min_length=64, max_length=64)
    specialist_artifact_references: tuple[ThesisSpecialistArtifactReference, ...] = ()
    analytical_bias: ThesisAnalyticalBias
    consensus_state: ThesisConsensusState
    evidence_coverage: ThesisEvidenceCoverage
    evidence_as_of: datetime
    limitations: tuple[str, ...] = ()
    invalidating_conditions: tuple[str, ...] = ()
    authorization_status: Literal[ThesisAuthorizationStatus.NON_EXECUTABLE_NON_AUTHORIZING] = (
        ThesisAuthorizationStatus.NON_EXECUTABLE_NON_AUTHORIZING
    )
    created_at: datetime
    expires_at: datetime | None = None

    @field_validator("evidence_as_of", "created_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("thesis timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator("evidence_references")
    @classmethod
    def order_evidence_references(
        cls, values: tuple[SpecialistEvidenceReference, ...]
    ) -> tuple[SpecialistEvidenceReference, ...]:
        unique = {
            (
                item.event_id,
                item.source_id,
                item.domain.value,
                item.timestamp.isoformat(),
                item.symbol or "",
            ): item
            for item in values
        }
        return tuple(unique[key] for key in sorted(unique))

    @field_validator("specialist_artifact_references")
    @classmethod
    def order_specialist_references(
        cls,
        values: tuple[ThesisSpecialistArtifactReference, ...],
    ) -> tuple[ThesisSpecialistArtifactReference, ...]:
        unique = {item.agent_id: item for item in values}
        if len(unique) != len(values):
            raise ValueError("thesis specialist artifact references must have unique agent IDs")
        return tuple(unique[key] for key in sorted(unique))

    @field_validator("limitations", "invalidating_conditions")
    @classmethod
    def canonicalize_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip() for value in values if value.strip()}))

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "thesis_id": self.thesis_id,
                "analysis_id": self.analysis_id,
                "symbol": self.symbol,
                "evidence_fingerprint": self.evidence_fingerprint,
                "evidence_references": [item.model_dump(mode="json") for item in self.evidence_references],
                "master_synthesis_id": self.master_synthesis_id,
                "master_synthesis_fingerprint": self.master_synthesis_fingerprint,
                "specialist_artifact_references": [
                    item.model_dump(mode="json") for item in self.specialist_artifact_references
                ],
                "analytical_bias": self.analytical_bias.value,
                "consensus_state": self.consensus_state.value,
                "evidence_coverage": self.evidence_coverage.model_dump(mode="json"),
                "evidence_as_of": self.evidence_as_of.isoformat(),
                "limitations": list(self.limitations),
                "invalidating_conditions": list(self.invalidating_conditions),
                "authorization_status": self.authorization_status.value,
                "created_at": self.created_at.isoformat(),
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            }
        )

    def as_reference(self) -> MasterTradeThesisReference:
        return MasterTradeThesisReference(thesis_id=self.thesis_id, fingerprint=self.fingerprint)


def thesis_identity(
    *,
    analysis_id: str,
    evidence_fingerprint: str,
    master_synthesis_id: str,
    master_synthesis_fingerprint: str,
) -> str:
    return "master-trade-thesis-" + _fingerprint(
        {
            "analysis_id": analysis_id,
            "evidence_fingerprint": evidence_fingerprint,
            "master_synthesis_id": master_synthesis_id,
            "master_synthesis_fingerprint": master_synthesis_fingerprint,
        }
    )


def stable_fingerprint(value: object) -> str:
    """Shared canonical hashing helper for the Master decision service."""
    return _fingerprint(value)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
