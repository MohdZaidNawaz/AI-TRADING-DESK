"""Immutable, provider-neutral Sentinel risk-policy contracts.

Phase 7A records the already-existing deterministic Sentinel rules as a
versioned policy artifact.  It intentionally contains no provider payloads,
account bindings, execution authority, or newly introduced thresholds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class SentinelPolicyModel(APIModel):
    """Frozen base for internal Sentinel policy contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MissingDataStrategy(str, Enum):
    """The only fail-closed outcomes available to a missing policy input."""

    INSUFFICIENT_DATA = "insufficient_data"
    REJECTED = "rejected"


class SentinelPolicyMetadata(SentinelPolicyModel):
    """Small immutable, semantic policy metadata entry."""

    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=160)

    @field_validator("key", "value", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("policy metadata values cannot be blank")
            return normalized
        return value


class SentinelPolicyRules(SentinelPolicyModel):
    """Explicit representation of the pre-existing Sentinel rule set only."""

    require_pending_risk_proposal: bool = True
    require_symbol: bool = True
    require_explicit_trade_parameters: bool = True
    require_matching_market_evidence: bool = True
    require_snapshot_provenance_match: bool = True
    require_unexpired_proposal: bool = True
    require_risk_context: bool = True
    require_portfolio_context: bool = True
    require_market_freshness_policy: bool = True
    require_requested_risk_limit: bool = True
    require_specialist_agreement: bool = True
    require_conflict_policy: bool = True
    require_approval_ttl: bool = True
    require_news_risk_assessment_when_news_present: bool = True


class SentinelRiskPolicy(SentinelPolicyModel):
    """One immutable semantic policy used to trace one Sentinel evaluation."""

    policy_id: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=80)
    effective_at: datetime
    expires_at: datetime | None = None
    missing_data_strategy: MissingDataStrategy = MissingDataStrategy.INSUFFICIENT_DATA
    rules: SentinelPolicyRules = SentinelPolicyRules()
    max_market_evidence_age_seconds: int | None = Field(default=None, ge=1)
    approval_ttl_seconds: int | None = Field(default=None, ge=1)
    max_requested_risk_fraction: FiniteFloat | None = None
    required_agreeing_specialists: int | None = Field(default=None, ge=1)
    reject_conflicting_signals: bool | None = None
    metadata: tuple[SentinelPolicyMetadata, ...] = ()

    @field_validator("policy_id", "policy_version", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("policy identity values cannot be blank")
            return normalized
        return value

    @field_validator("effective_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("policy timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator("metadata")
    @classmethod
    def canonicalize_metadata(
        cls, values: tuple[SentinelPolicyMetadata, ...]
    ) -> tuple[SentinelPolicyMetadata, ...]:
        by_key = {item.key: item for item in values}
        if len(by_key) != len(values):
            raise ValueError("policy metadata keys must be unique")
        return tuple(by_key[key] for key in sorted(by_key))

    @model_validator(mode="after")
    def validate_semantics(self) -> "SentinelRiskPolicy":
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("policy expiry must be after its effective time")
        if self.max_requested_risk_fraction is not None and self.max_requested_risk_fraction <= 0:
            raise ValueError("maximum requested risk fraction must be positive")
        return self

    def is_effective_at(self, evaluated_at: datetime) -> bool:
        """Return whether this immutable policy is usable at ``evaluated_at``."""
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("policy evaluation time must include a timezone")
        normalized = evaluated_at.astimezone(UTC)
        return normalized >= self.effective_at and (
            self.expires_at is None or normalized < self.expires_at
        )

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        """Deterministic fingerprint of every policy-semantic field."""
        return _fingerprint(
            {
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "effective_at": self.effective_at.isoformat(),
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "missing_data_strategy": self.missing_data_strategy.value,
                "rules": self.rules.model_dump(mode="json"),
                "max_market_evidence_age_seconds": self.max_market_evidence_age_seconds,
                "approval_ttl_seconds": self.approval_ttl_seconds,
                "max_requested_risk_fraction": self.max_requested_risk_fraction,
                "required_agreeing_specialists": self.required_agreeing_specialists,
                "reject_conflicting_signals": self.reject_conflicting_signals,
                "metadata": [item.model_dump(mode="json") for item in self.metadata],
            }
        )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
