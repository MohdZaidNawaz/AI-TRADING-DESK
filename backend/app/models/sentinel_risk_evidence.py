"""Immutable, non-authorizing verification record for Sentinel risk inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class SentinelRiskEvidenceStatus(str, Enum):
    VALIDATED = "validated"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class SentinelRiskEvidenceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SentinelPortfolioRiskEvidence(SentinelRiskEvidenceModel):
    """A recomputable policy result, never a Sentinel approval or order permit."""

    status: SentinelRiskEvidenceStatus
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    calculation_version: str = Field(min_length=1, max_length=80)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    analysis_id: str = Field(min_length=1, max_length=120)
    evidence_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(min_length=1, max_length=160)
    approval_id: str = Field(min_length=1, max_length=160)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=160)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    risk_currency: str = Field(min_length=1, max_length=16)
    trusted_evidence_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    position_snapshot_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    candidate_monetary_risk: Decimal | None = None
    existing_monetary_risk: Decimal | None = None
    order_notional: Decimal | None = None
    current_open_volume: Decimal | None = None
    evaluated_at: datetime
    reason_codes: tuple[str, ...] = ()
    execution_authority: bool = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Sentinel risk evidence timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_status(self) -> "SentinelPortfolioRiskEvidence":
        if self.execution_authority is not False:
            raise ValueError("Sentinel risk evidence cannot grant execution authority")
        if self.status is SentinelRiskEvidenceStatus.VALIDATED and self.reason_codes:
            raise ValueError("validated Sentinel risk evidence cannot contain failure codes")
        if self.status is not SentinelRiskEvidenceStatus.VALIDATED and not self.reason_codes:
            raise ValueError("unavailable or rejected Sentinel risk evidence requires reason codes")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return sha256(
            json.dumps(self.model_dump(mode="json", exclude={"fingerprint"}), sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
