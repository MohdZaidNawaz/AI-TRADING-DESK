"""Provider-neutral contracts for external trust attestations.

These immutable contracts validate signed structure and lineage.  They cannot
establish independent trust because this repository has no independently
provisioned verification root or operational external authority.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class ExternalTrustModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalTrustCapability(str, Enum):
    ROOT_PROVISIONING = "root_provisioning"
    LIFECYCLE_STATUS = "lifecycle_status"
    BROKER_EVIDENCE_AUTHENTICATION = "broker_evidence_authentication"


class ExternalTrustLifecycle(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    RECOVERY_REQUIRED = "recovery_required"


class ExternalReconciliationStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"
    UNRESOLVED = "unresolved"


class ExternalTrustIntegrationStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    CONFIGURED_UNAUTHENTICATED = "configured_unauthenticated"


class ExternalTrustAttestationRequest(ExternalTrustModel):
    schema_version: Literal["external-trust-attestation-request/v1"] = (
        "external-trust-attestation-request/v1"
    )
    request_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    expected_provider_id: str = Field(min_length=1, max_length=160)
    capability: ExternalTrustCapability
    subject_id: str = Field(min_length=1, max_length=160)
    subject_generation: int = Field(ge=1)
    subject_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=80)
    minimum_source_sequence: int = Field(ge=0)
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)

    @field_validator("expected_provider_id", "subject_id", "policy_version", mode="before")
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


class ExternalTrustAttestationStatement(ExternalTrustModel):
    schema_version: Literal["external-trust-attestation-statement/v1"] = (
        "external-trust-attestation-statement/v1"
    )
    statement_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    provider_id: str = Field(min_length=1, max_length=160)
    verification_authority_id: str = Field(min_length=1, max_length=160)
    verification_root_id: str = Field(min_length=1, max_length=160)
    verification_root_version: str = Field(min_length=1, max_length=80)
    verification_root_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    signature_algorithm: Literal["ED25519"] = "ED25519"
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    capability: ExternalTrustCapability
    subject_id: str = Field(min_length=1, max_length=160)
    subject_generation: int = Field(ge=1)
    subject_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=80)
    source_sequence: int = Field(gt=0)
    issued_at: datetime
    observed_at: datetime
    retrieved_at: datetime
    expires_at: datetime
    lifecycle: ExternalTrustLifecycle | None = None
    reconciliation_status: ExternalReconciliationStatus = ExternalReconciliationStatus.NOT_APPLICABLE
    evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reason_codes: tuple[str, ...] = ()

    @field_validator(
        "provider_id", "verification_authority_id", "verification_root_id", "verification_root_version",
        "subject_id", "policy_version", mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("issued_at", "observed_at", "retrieved_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        codes = tuple(sorted(set(value)))
        if any(not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", code) is None for code in codes):
            raise ValueError("external trust reason codes must be canonical")
        return codes

    @model_validator(mode="after")
    def validate_statement(self) -> "ExternalTrustAttestationStatement":
        if not self.issued_at <= self.observed_at <= self.retrieved_at < self.expires_at:
            raise ValueError("external attestation chronology is inconsistent")
        if self.capability in {
            ExternalTrustCapability.ROOT_PROVISIONING,
            ExternalTrustCapability.LIFECYCLE_STATUS,
        }:
            if self.lifecycle is None or self.reconciliation_status is not ExternalReconciliationStatus.NOT_APPLICABLE:
                raise ValueError("governance attestations require lifecycle-only state")
        elif (
            self.lifecycle is not None
            or self.evidence_fingerprint is None
            or self.reconciliation_status is ExternalReconciliationStatus.NOT_APPLICABLE
        ):
            raise ValueError("broker attestations require evidence and reconciliation state")
        if self.lifecycle is ExternalTrustLifecycle.ACTIVE and self.reason_codes:
            raise ValueError("active lifecycle attestations cannot contain failure reasons")
        if self.lifecycle is not None and self.lifecycle is not ExternalTrustLifecycle.ACTIVE and not self.reason_codes:
            raise ValueError("non-active lifecycle attestations require failure reasons")
        if self.reconciliation_status is ExternalReconciliationStatus.UNRESOLVED and not self.reason_codes:
            raise ValueError("unresolved reconciliation requires failure reasons")
        return self

    @computed_field(return_type=str)
    @property
    def statement_fingerprint(self) -> str:
        return sha256(canonical_external_attestation_payload(self)).hexdigest()


class SignedExternalTrustAttestation(ExternalTrustModel):
    schema_version: Literal["signed-external-trust-attestation/v1"] = (
        "signed-external-trust-attestation/v1"
    )
    statement: ExternalTrustAttestationStatement
    signature_base64: str = Field(min_length=88, max_length=88)

    @field_validator("signature_base64")
    @classmethod
    def valid_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError("external attestation signature is malformed") from error
        if len(decoded) != 64:
            raise ValueError("external attestation requires an Ed25519 signature")
        return value


class ExternalTrustAttestationAssessment(ExternalTrustModel):
    schema_version: Literal["external-trust-attestation-assessment/v1"] = (
        "external-trust-attestation-assessment/v1"
    )
    status: ExternalTrustIntegrationStatus
    capability: ExternalTrustCapability
    provider_id: str = Field(min_length=1, max_length=160)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    statement_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_sequence: int | None = Field(default=None, gt=0)
    signature_verified: bool = False
    bindings_verified: bool = False
    reason_codes: tuple[str, ...] = ()
    independently_authenticated: Literal[False] = False
    authoritative_lifecycle: Literal[False] = False
    external_rollback_assurance: Literal[False] = False
    broker_evidence_authenticated: Literal[False] = False
    reconciliation_authoritative: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def enforce_non_authority(self) -> "ExternalTrustAttestationAssessment":
        verified = self.signature_verified or self.bindings_verified
        if verified and (self.statement_fingerprint is None or self.source_sequence is None):
            raise ValueError("verified attestations require immutable statement lineage")
        if self.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED:
            if not (self.signature_verified and self.bindings_verified):
                raise ValueError("configured attestation requires signature and binding verification")
            if self.reason_codes != ("EXTERNAL_VERIFICATION_ROOT_NOT_INDEPENDENTLY_AUTHENTICATED",):
                raise ValueError("configured attestation must disclose unavailable independent trust")
        elif not self.reason_codes:
            raise ValueError("unsuccessful external trust assessments require reason codes")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def canonical_external_attestation_payload(statement: ExternalTrustAttestationStatement) -> bytes:
    return json.dumps(
        statement.model_dump(mode="json", exclude={"statement_fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external trust timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
