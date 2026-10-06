"""Immutable, provider-neutral Sentinel risk-assessment traceability contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel
from app.models.portfolio_risk import SourceAccountKey


class RiskAssessmentModel(APIModel):
    """Frozen base for non-authorizing Sentinel assessment artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskCheckStatus(str, Enum):
    PASS = "pass"
    REJECTED = "rejected"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_APPLICABLE = "not_applicable"


class EventRiskStatus(str, Enum):
    ASSESSED_CLEAR = "assessed_clear"
    ASSESSED_RELEVANT = "assessed_relevant"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_APPLICABLE = "not_applicable"


class SentinelAccountBinding(RiskAssessmentModel):
    """Exact source-scoped account and compatible snapshot selection."""

    account: SourceAccountKey
    portfolio_snapshot_id: str = Field(min_length=1, max_length=160)
    portfolio_snapshot_fingerprint: str = Field(min_length=64, max_length=64)
    metrics_snapshot_id: str = Field(min_length=1, max_length=160)
    metrics_snapshot_fingerprint: str = Field(min_length=64, max_length=64)


class SentinelEventRiskAssessment(RiskAssessmentModel):
    """Factual event-evidence assessment; never a trade recommendation."""

    domain: str = Field(pattern=r"^(news|economic_event|corporate_action)$")
    status: EventRiskStatus
    evidence_references: tuple["RiskCheckInputReference", ...] = ()
    reason_codes: tuple[str, ...] = ()

    @field_validator("evidence_references")
    @classmethod
    def order_event_references(
        cls, values: tuple["RiskCheckInputReference", ...]
    ) -> tuple["RiskCheckInputReference", ...]:
        return tuple(sorted(set(values), key=lambda item: (item.reference_type, item.reference_id)))

    @field_validator("reason_codes")
    @classmethod
    def order_event_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip() for value in values if value.strip()}))

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "domain": self.domain,
                "status": self.status.value,
                "evidence_references": [item.model_dump(mode="json") for item in self.evidence_references],
                "reason_codes": list(self.reason_codes),
            }
        )


class RiskCheckInputReference(RiskAssessmentModel):
    """A compact, safe reference to an input used by one policy check."""

    reference_type: str = Field(min_length=1, max_length=80)
    reference_id: str = Field(min_length=1, max_length=160)
    fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("reference_type", "reference_id", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("risk assessment reference values cannot be blank")
            return normalized
        return value


class RiskCheckResult(RiskAssessmentModel):
    """One factual policy-check outcome; it has no approval authority."""

    check_name: str = Field(min_length=1, max_length=120)
    status: RiskCheckStatus
    reason_codes: tuple[str, ...] = ()
    reason: str | None = Field(default=None, max_length=500)
    input_references: tuple[RiskCheckInputReference, ...] = ()

    @field_validator("check_name", "reason", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("reason_codes")
    @classmethod
    def canonicalize_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = {value.strip() for value in values if value.strip()}
        return tuple(sorted(normalized))

    @field_validator("input_references")
    @classmethod
    def canonicalize_references(
        cls, values: tuple[RiskCheckInputReference, ...]
    ) -> tuple[RiskCheckInputReference, ...]:
        unique = {
            (item.reference_type, item.reference_id, item.fingerprint or ""): item
            for item in values
        }
        return tuple(unique[key] for key in sorted(unique))

    @computed_field(return_type=str)
    @property
    def check_id(self) -> str:
        """Stable identity of a check definition and its immutable inputs."""
        return "risk-check-" + _fingerprint(
            {
                "check_name": self.check_name,
                "input_references": [item.model_dump(mode="json") for item in self.input_references],
            }
        )

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "check_id": self.check_id,
                "status": self.status.value,
                "reason_codes": list(self.reason_codes),
                "reason": self.reason,
            }
        )


class SentinelRiskAssessment(RiskAssessmentModel):
    """Immutable collection of deterministic Sentinel policy-check outcomes."""

    policy_id: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    evidence_references: tuple[RiskCheckInputReference, ...] = ()
    account_binding: SentinelAccountBinding | None = None
    event_assessments: tuple[SentinelEventRiskAssessment, ...] = ()
    checks: tuple[RiskCheckResult, ...] = ()
    evaluated_at: datetime
    overall_status: RiskCheckStatus

    @field_validator("policy_id", "policy_version", "proposal_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("risk assessment identity values cannot be blank")
            return normalized
        return value

    @field_validator("evaluated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("assessment evaluation time must include a timezone")
        return value.astimezone(UTC)

    @field_validator("evidence_references")
    @classmethod
    def canonicalize_evidence_references(
        cls, values: tuple[RiskCheckInputReference, ...]
    ) -> tuple[RiskCheckInputReference, ...]:
        unique = {
            (item.reference_type, item.reference_id, item.fingerprint or ""): item
            for item in values
        }
        return tuple(unique[key] for key in sorted(unique))

    @field_validator("checks")
    @classmethod
    def canonicalize_checks(cls, values: tuple[RiskCheckResult, ...]) -> tuple[RiskCheckResult, ...]:
        unique = {item.check_id: item for item in values}
        if len(unique) != len(values):
            raise ValueError("risk assessment checks must have unique deterministic IDs")
        return tuple(unique[key] for key in sorted(unique))

    @field_validator("event_assessments")
    @classmethod
    def canonicalize_event_assessments(
        cls, values: tuple[SentinelEventRiskAssessment, ...]
    ) -> tuple[SentinelEventRiskAssessment, ...]:
        unique = {item.domain: item for item in values}
        if len(unique) != len(values):
            raise ValueError("event risk assessments must have unique domains")
        return tuple(unique[key] for key in sorted(unique))

    @model_validator(mode="after")
    def validate_overall_status(self) -> "SentinelRiskAssessment":
        expected = _overall_status(self.checks)
        if self.overall_status is not expected:
            raise ValueError("assessment overall status must match its check outcomes")
        return self

    @computed_field(return_type=str)
    @property
    def assessment_id(self) -> str:
        return "sentinel-risk-assessment-" + _fingerprint(
            {
                "policy_id": self.policy_id,
                "policy_fingerprint": self.policy_fingerprint,
                "proposal_id": self.proposal_id,
                "proposal_fingerprint": self.proposal_fingerprint,
                "evidence_fingerprint": self.evidence_fingerprint,
                "evaluated_at": self.evaluated_at.isoformat(),
            }
        )

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "assessment_id": self.assessment_id,
                "policy_version": self.policy_version,
                "evidence_references": [
                    item.model_dump(mode="json") for item in self.evidence_references
                ],
                "account_binding": (
                    self.account_binding.model_dump(mode="json") if self.account_binding else None
                ),
                "event_assessments": [item.fingerprint for item in self.event_assessments],
                "checks": [item.fingerprint for item in self.checks],
                "overall_status": self.overall_status.value,
            }
        )


def _overall_status(checks: tuple[RiskCheckResult, ...]) -> RiskCheckStatus:
    if any(item.status is RiskCheckStatus.REJECTED for item in checks):
        return RiskCheckStatus.REJECTED
    if any(item.status is RiskCheckStatus.INSUFFICIENT_DATA for item in checks):
        return RiskCheckStatus.INSUFFICIENT_DATA
    if not checks or all(item.status is RiskCheckStatus.NOT_APPLICABLE for item in checks):
        return RiskCheckStatus.NOT_APPLICABLE
    return RiskCheckStatus.PASS


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
