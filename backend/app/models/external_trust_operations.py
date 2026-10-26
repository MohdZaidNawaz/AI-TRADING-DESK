"""Phase 19 operational external-trust composition contracts.

The models retain only validated fingerprints and diagnostic state.  They do
not turn configured transport, local signatures, or test providers into an
independent authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class ExternalOperationsModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalOperationsStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    CONFLICT = "conflict"
    STRUCTURALLY_VALIDATED_UNAUTHENTICATED = "structurally_validated_unauthenticated"


class ExternalTransportContext(ExternalOperationsModel):
    """Configured transport policy; it is not proof of a successful handshake."""

    schema_version: Literal["external-transport-context/v1"] = (
        "external-transport-context/v1"
    )
    provider_id: str = Field(min_length=1, max_length=160)
    protocol_version: Literal["FRIDAY-EXTERNAL-TRUST/1"] = "FRIDAY-EXTERNAL-TRUST/1"
    endpoint_reference: str = Field(min_length=1, max_length=200)
    connect_timeout_seconds: float = Field(gt=0, le=10)
    read_timeout_seconds: float = Field(gt=0, le=30)
    overall_timeout_seconds: float = Field(gt=0, le=30)
    maximum_concurrency: int = Field(gt=0, le=16)
    automatic_retries: Literal[0] = 0
    configured: bool = False
    transport_authenticated: Literal[False] = False
    reason_codes: tuple[str, ...]

    @field_validator("provider_id", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("endpoint_reference")
    @classmethod
    def safe_endpoint_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or re.search(
            r"(?i)(password|secret|token|api[_-]?key|credential)\s*[:=]", normalized
        ):
            raise ValueError("transport endpoint reference must be sanitized")
        return normalized

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_reasons(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_transport(self) -> "ExternalTransportContext":
        if self.overall_timeout_seconds < max(
            self.connect_timeout_seconds, self.read_timeout_seconds
        ):
            raise ValueError("overall timeout cannot be shorter than component timeouts")
        if not self.reason_codes:
            raise ValueError("non-authoritative transport requires an explicit blocker")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class ExternalTrustOperationalAssessment(ExternalOperationsModel):
    """End-to-end diagnostic that is structurally unable to authorize execution."""

    schema_version: Literal["external-trust-operational-assessment/v1"] = (
        "external-trust-operational-assessment/v1"
    )
    status: ExternalOperationsStatus
    provider_id: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    transport_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    root_assessment_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    lifecycle_assessment_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    witness_assessment_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    broker_assessment_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reconciliation_assessment_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    local_bindings_verified: bool = False
    replay_consumed: bool = False
    reason_codes: tuple[str, ...]
    independent_root_provisioned: Literal[False] = False
    authoritative_lifecycle_available: Literal[False] = False
    transport_authenticated: Literal[False] = False
    external_rollback_assurance: Literal[False] = False
    broker_evidence_authenticated: Literal[False] = False
    reconciliation_authoritative: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("provider_id", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("external operations timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_reasons(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_diagnostic(self) -> "ExternalTrustOperationalAssessment":
        lineage = (
            self.transport_fingerprint,
            self.root_assessment_fingerprint,
            self.lifecycle_assessment_fingerprint,
            self.witness_assessment_fingerprint,
            self.broker_assessment_fingerprint,
            self.reconciliation_assessment_fingerprint,
        )
        if self.status is ExternalOperationsStatus.STRUCTURALLY_VALIDATED_UNAUTHENTICATED:
            if (
                any(item is None for item in lineage)
                or not self.local_bindings_verified
                or not self.replay_consumed
                or self.reason_codes
                != ("EXTERNAL_OPERATIONAL_AUTHORITIES_NOT_INDEPENDENTLY_PROVISIONED",)
            ):
                raise ValueError("structural validation requires complete local lineage and blocker")
        elif self.local_bindings_verified or self.replay_consumed:
            raise ValueError("failed external operations cannot advertise verified state")
        if not self.reason_codes:
            raise ValueError("external operations diagnostics require reason codes")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def _reason_codes(value: object) -> object:
    if not isinstance(value, (tuple, list)):
        return value
    normalized = tuple(sorted(set(value)))
    if any(
        not isinstance(item, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None
        for item in normalized
    ):
        raise ValueError("external operations reasons must be canonical identifiers")
    return normalized


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
