"""Provider-neutral, non-authorizing external trust observations.

These contracts describe what a future independently operated policy authority
and broker-reconciliation provider must return.  They intentionally do not
establish authority: constructing a model, implementing the provider protocol,
or passing structural validation cannot enable execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.execution_state import BrokerOrderObservation
from app.models.schemas import APIModel
from app.models.sentinel_policy_approval import SentinelPolicyApprovalState
from app.models.sentinel_policy_governance import SentinelPolicyGovernanceRecord


class IndependentGovernanceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalAuthorityAvailability(str, Enum):
    AVAILABLE = "available"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class ExternalAssessmentStatus(str, Enum):
    STRUCTURALLY_VALIDATED = "structurally_validated"
    CONFIRMED_ABSENT = "confirmed_absent"
    INVALID = "invalid"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class IndependentPolicyAuthorityRequest(IndependentGovernanceModel):
    schema_version: Literal["independent-policy-authority-request/v1"] = (
        "independent-policy-authority-request/v1"
    )
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=160)
    execution_mode: str = Field(min_length=1, max_length=32)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    risk_currency: str = Field(min_length=3, max_length=12)
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)

    @field_validator(
        "policy_id", "policy_version", "account_id", "execution_mode",
        "canonical_symbol", "risk_currency", mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @computed_field(return_type=str)
    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"request_fingerprint"}))


class IndependentPolicyAuthorityObservation(IndependentGovernanceModel):
    """Sanitized source response.  It is evidence to validate, not authority."""

    schema_version: Literal["independent-policy-authority-observation/v1"] = (
        "independent-policy-authority-observation/v1"
    )
    availability: ExternalAuthorityAvailability
    provider_id: str = Field(min_length=1, max_length=160)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    source_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, gt=0)
    observed_at: datetime | None = None
    retrieved_at: datetime
    governance: SentinelPolicyGovernanceRecord | None = None
    approval: SentinelPolicyApprovalState | None = None
    trust_anchor_generation: int | None = Field(default=None, gt=0)
    previous_trust_anchor_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    audit_reference: str | None = Field(default=None, min_length=1, max_length=240)
    reason_codes: tuple[str, ...] = ()
    authorizes_execution: Literal[False] = False

    @field_validator("provider_id", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("source_event_id", "audit_reference", mode="before")
    @classmethod
    def safe_reference(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if any(marker in normalized.lower() for marker in _SENSITIVE_MARKERS):
            raise ValueError("external references cannot contain sensitive material")
        return normalized

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_observation(self) -> "IndependentPolicyAuthorityObservation":
        required = (
            self.source_event_id,
            self.source_sequence,
            self.observed_at,
            self.governance,
            self.approval,
            self.trust_anchor_generation,
            self.audit_reference,
        )
        if self.availability is ExternalAuthorityAvailability.AVAILABLE:
            if any(value is None for value in required) or self.reason_codes:
                raise ValueError("available authority observations require complete facts and no failures")
            anchor = self.governance.trust_anchor if self.governance is not None else None
            if anchor is None:
                raise ValueError("available authority observations require trust-anchor metadata")
            if self.trust_anchor_generation == 1 and self.previous_trust_anchor_fingerprint is not None:
                raise ValueError("first trust-anchor generation cannot name a predecessor")
            if self.trust_anchor_generation is not None and self.trust_anchor_generation > 1:
                if (
                    self.previous_trust_anchor_fingerprint is None
                    or self.previous_trust_anchor_fingerprint == anchor.public_identity_fingerprint
                ):
                    raise ValueError("rotated trust anchors require a distinct predecessor fingerprint")
        elif any(value is not None for value in required) or self.previous_trust_anchor_fingerprint is not None or not self.reason_codes:
            raise ValueError("unavailable authority observations require failures and no authority facts")
        return self

    @computed_field(return_type=str)
    @property
    def observation_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"observation_fingerprint"}))


class IndependentPolicyAuthorityAssessment(IndependentGovernanceModel):
    schema_version: Literal["independent-policy-authority-assessment/v1"] = (
        "independent-policy-authority-assessment/v1"
    )
    status: ExternalAssessmentStatus
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    observation_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, gt=0)
    governance_record_id: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    approval_artifact_id: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reason_codes: tuple[str, ...] = ()
    independently_authoritative: Literal[False] = False
    production_authorization_available: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "IndependentPolicyAuthorityAssessment":
        lineage = (
            self.observation_fingerprint,
            self.source_event_id,
            self.source_sequence,
            self.governance_record_id,
            self.approval_artifact_id,
        )
        if self.status is ExternalAssessmentStatus.STRUCTURALLY_VALIDATED:
            if any(value is None for value in lineage) or self.reason_codes:
                raise ValueError("structural validation requires complete lineage and no failures")
        elif not self.reason_codes:
            raise ValueError("failed authority assessments require reason codes")
        return self


class BrokerReconciliationRequest(IndependentGovernanceModel):
    schema_version: Literal["broker-reconciliation-request/v1"] = "broker-reconciliation-request/v1"
    execution_id: str = Field(min_length=1, max_length=160)
    execution_record_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    attempt_identity: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=160)
    account_reference: str = Field(min_length=1, max_length=160)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    broker_order_id: str = Field(min_length=1, max_length=160)
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)

    @field_validator(
        "account_id", "account_reference", "canonical_symbol", "broker_symbol", mode="before"
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @computed_field(return_type=str)
    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"request_fingerprint"}))


class BrokerReconciliationObservation(IndependentGovernanceModel):
    """Read-only sanitized broker fact; never a retry or execution grant."""

    schema_version: Literal["broker-reconciliation-observation/v1"] = (
        "broker-reconciliation-observation/v1"
    )
    availability: ExternalAuthorityAvailability
    provider_id: str = Field(min_length=1, max_length=160)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    source_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, gt=0)
    account_reference: str | None = Field(default=None, min_length=1, max_length=160)
    observed_at: datetime | None = None
    retrieved_at: datetime
    broker_order: BrokerOrderObservation | None = None
    audit_reference: str | None = Field(default=None, min_length=1, max_length=240)
    reason_codes: tuple[str, ...] = ()
    authorizes_execution: Literal[False] = False

    @field_validator("provider_id", "account_reference", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("source_event_id", "audit_reference", mode="before")
    @classmethod
    def safe_reference(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if any(marker in normalized.lower() for marker in _SENSITIVE_MARKERS):
            raise ValueError("external references cannot contain sensitive material")
        return normalized

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_observation(self) -> "BrokerReconciliationObservation":
        facts = (
            self.source_event_id,
            self.source_sequence,
            self.account_reference,
            self.observed_at,
            self.audit_reference,
        )
        if self.availability is ExternalAuthorityAvailability.AVAILABLE:
            if any(value is None for value in facts) or self.broker_order is None or self.reason_codes:
                raise ValueError("available broker observations require complete facts and no failures")
        elif self.availability is ExternalAuthorityAvailability.NOT_FOUND:
            if any(value is None for value in facts) or self.broker_order is not None or self.reason_codes:
                raise ValueError("confirmed absence requires complete provenance and no broker order")
        elif any(value is not None for value in facts) or self.broker_order is not None or not self.reason_codes:
            raise ValueError("unavailable broker observations require failures and no broker facts")
        return self

    @computed_field(return_type=str)
    @property
    def observation_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"observation_fingerprint"}))


class BrokerReconciliationAssessment(IndependentGovernanceModel):
    schema_version: Literal["broker-reconciliation-assessment/v1"] = (
        "broker-reconciliation-assessment/v1"
    )
    status: ExternalAssessmentStatus
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    observation_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, gt=0)
    broker_order: BrokerOrderObservation | None = None
    reason_codes: tuple[str, ...] = ()
    authoritative_reconciliation_established: Literal[False] = False
    retry_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "BrokerReconciliationAssessment":
        source_lineage = (self.observation_fingerprint, self.source_event_id, self.source_sequence)
        successful = self.status in {
            ExternalAssessmentStatus.STRUCTURALLY_VALIDATED,
            ExternalAssessmentStatus.CONFIRMED_ABSENT,
        }
        if successful and (any(value is None for value in source_lineage) or self.reason_codes):
            raise ValueError("structural broker assessment requires complete lineage and no failures")
        if self.status is ExternalAssessmentStatus.STRUCTURALLY_VALIDATED and self.broker_order is None:
            raise ValueError("validated broker assessment requires an order observation")
        if self.status is ExternalAssessmentStatus.CONFIRMED_ABSENT and self.broker_order is not None:
            raise ValueError("confirmed absence cannot include an order observation")
        if not successful and not self.reason_codes:
            raise ValueError("failed broker assessment requires reason codes")
        return self


_SENSITIVE_MARKERS = ("password", "private_key", "secret=", "token=", "credential=")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external trust timestamps require timezone awareness")
    return value.astimezone(UTC)


def _reason_codes(value: object) -> object:
    if not isinstance(value, (tuple, list)):
        return value
    normalized = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
    if any(len(item) > 120 or not item.replace("_", "").isalnum() for item in normalized):
        raise ValueError("reason codes must be canonical bounded identifiers")
    return normalized


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
