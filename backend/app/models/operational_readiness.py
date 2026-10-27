"""Immutable, provider-neutral operational-readiness diagnostics.

These contracts describe evidence and deployment prerequisites. They never
grant production authority, submission permission, or execution authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


_FINGERPRINT = r"^[0-9a-f]{64}$"
_REASON = r"^[A-Z][A-Z0-9_]{0,119}$"
_SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|password|secret|token|credential|authorization|private[_-]?key)\s*[:=]"
)


class OperationalReadinessModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OperationalReadinessPrerequisite(str, Enum):
    APPLICATION_CONFIGURATION = "application_configuration"
    INDEPENDENT_TRUST_ROOT = "independent_trust_root"
    LIFECYCLE_REVOCATION = "lifecycle_revocation"
    EXTERNAL_ROLLBACK_WITNESS = "external_rollback_witness"
    BROKER_EVIDENCE = "broker_evidence"
    AUTHORITATIVE_RECONCILIATION = "authoritative_reconciliation"
    DURABLE_REPLAY_STORAGE = "durable_replay_storage"
    CLOCK_AND_FRESHNESS = "clock_and_freshness"
    PROVIDER_LIMITS = "provider_limits"
    AUDIT_AND_REDACTION = "audit_and_redaction"
    MONITORING_AND_ALERTING = "monitoring_and_alerting"
    BACKUP_RECOVERY_INCIDENT_RESPONSE = "backup_recovery_incident_response"
    INDEPENDENT_SECURITY_REVIEW = "independent_security_review"
    HUMAN_PRODUCTION_APPROVAL = "human_production_approval"
    EXECUTION_CONTROLS = "execution_controls"


class OperationalReadinessStatus(str, Enum):
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    INVALID = "invalid"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    NOT_ASSESSED = "not_assessed"


class OperationalEvidenceSource(str, Enum):
    LOCAL_CLAIM = "local_claim"
    SYNTHETIC_TEST = "synthetic_test"
    EXTERNAL_CLAIM = "external_claim"
    MANUAL_CLAIM = "manual_claim"


class OperationalRuntimeConfiguration(OperationalReadinessModel):
    """Non-secret bounded runtime policy; configuration is not authority."""

    schema_version: Literal["operational-runtime-configuration/v1"] = (
        "operational-runtime-configuration/v1"
    )
    configuration_revision: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    execution_mode: Literal["disabled"] = "disabled"
    provider_connect_timeout_seconds: FiniteFloat = Field(ge=0.05, le=10)
    provider_read_timeout_seconds: FiniteFloat = Field(ge=0.05, le=30)
    provider_overall_timeout_seconds: FiniteFloat = Field(ge=0.05, le=30)
    provider_maximum_concurrency: int = Field(ge=1, le=32)
    maximum_clock_skew_seconds: int = Field(ge=0, le=300)
    freshness_max_age_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def coherent_limits(self) -> "OperationalRuntimeConfiguration":
        if self.provider_overall_timeout_seconds < max(
            self.provider_connect_timeout_seconds,
            self.provider_read_timeout_seconds,
        ):
            raise ValueError("overall provider timeout must cover component timeouts")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(mode="json", exclude={"fingerprint"}),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()


class OperationalReadinessEvidence(OperationalReadinessModel):
    """Sanitized evidence metadata; never proof of external authority by itself."""

    schema_version: Literal["operational-readiness-evidence/v1"] = (
        "operational-readiness-evidence/v1"
    )
    prerequisite: OperationalReadinessPrerequisite
    status: OperationalReadinessStatus
    source: OperationalEvidenceSource
    source_reference: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    assessed_at: datetime
    expires_at: datetime | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator("assessed_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operational evidence timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(re.fullmatch(_REASON, code) is None for code in normalized):
            raise ValueError("operational evidence reason codes must be canonical")
        return normalized

    @field_validator("source_reference")
    @classmethod
    def safe_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("operational evidence references require printable text")
        if _SENSITIVE.search(normalized):
            raise ValueError("operational evidence references cannot contain secrets")
        return normalized

    @model_validator(mode="after")
    def coherent_evidence(self) -> "OperationalReadinessEvidence":
        if self.expires_at is not None and self.expires_at <= self.assessed_at:
            raise ValueError("operational evidence expiry must follow assessment")
        if self.status is OperationalReadinessStatus.SATISFIED:
            if self.reason_codes:
                raise ValueError("satisfied operational evidence cannot contain failures")
            if self.source_reference is None or self.evidence_fingerprint is None:
                raise ValueError("satisfied operational evidence requires sanitized provenance")
        elif not self.reason_codes:
            raise ValueError("unsatisfied operational evidence requires reason codes")
        return self


class OperationalReadinessCheck(OperationalReadinessModel):
    prerequisite: OperationalReadinessPrerequisite
    status: OperationalReadinessStatus
    reason_codes: tuple[str, ...]
    source_reference: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    checked_at: datetime
    authorizes_execution: Literal[False] = False

    @field_validator("checked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operational readiness timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(re.fullmatch(_REASON, code) is None for code in normalized):
            raise ValueError("operational readiness reason codes must be canonical")
        return normalized

    @field_validator("source_reference")
    @classmethod
    def safe_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("operational readiness references require printable text")
        if _SENSITIVE.search(normalized):
            raise ValueError("operational readiness references cannot contain secrets")
        return normalized

    @model_validator(mode="after")
    def coherent_check(self) -> "OperationalReadinessCheck":
        if self.status is OperationalReadinessStatus.SATISFIED:
            if (
                self.reason_codes
                or self.source_reference is None
                or self.evidence_fingerprint is None
            ):
                raise ValueError(
                    "satisfied readiness checks require fingerprinted provenance and no failures"
                )
        elif not self.reason_codes:
            raise ValueError("unsatisfied readiness checks require reason codes")
        return self


class OperationalReadinessReport(OperationalReadinessModel):
    """Complete diagnostic report that is structurally unable to authorize."""

    schema_version: Literal["operational-readiness-report/v1"] = (
        "operational-readiness-report/v1"
    )
    assessment_kind: Literal["offline_operational_acceptance"] = (
        "offline_operational_acceptance"
    )
    status: Literal["blocked"] = "blocked"
    evaluated_at: datetime
    checks: tuple[OperationalReadinessCheck, ...]
    reason_codes: tuple[str, ...]
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operational readiness timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(re.fullmatch(_REASON, code) is None for code in normalized):
            raise ValueError("operational readiness reason codes must be canonical")
        return normalized

    @model_validator(mode="after")
    def complete_report(self) -> "OperationalReadinessReport":
        expected = set(OperationalReadinessPrerequisite)
        actual = [check.prerequisite for check in self.checks]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("operational readiness requires every prerequisite exactly once")
        if tuple(actual) != tuple(sorted(actual, key=lambda item: item.value)):
            raise ValueError("operational readiness checks require deterministic ordering")
        if any(check.checked_at != self.evaluated_at for check in self.checks):
            raise ValueError("operational readiness checks require one evaluation timestamp")
        failures = {
            code
            for check in self.checks
            if check.status is not OperationalReadinessStatus.SATISFIED
            for code in check.reason_codes
        }
        if not failures or set(self.reason_codes) != failures:
            raise ValueError("operational readiness reasons must exactly summarize failures")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(mode="json", exclude={"fingerprint"}),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode()
        ).hexdigest()
