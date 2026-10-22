"""Immutable contracts for independent anchor governance and rollback witnesses.

These contracts deliberately separate cryptographic structure from independent
provenance.  The repository has no independently authenticated governance root
or external monotonic witness, so every result remains non-authorizing.
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

from app.models.personal_trust_bootstrap import PersonalTrustScope
from app.models.schemas import APIModel


class IndependentTrustGovernanceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrustAnchorLifecycle(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    ROTATION_PENDING = "rotation_pending"
    SUPERSEDED = "superseded"


class IndependentTrustGovernanceStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    CONFIGURED_UNAUTHENTICATED = "configured_unauthenticated"
    VALID_INDEPENDENTLY_AUTHENTICATED = "valid_independently_authenticated"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INVALID = "invalid"
    ROTATION_PENDING = "rotation_pending"
    GOVERNANCE_UNAVAILABLE = "governance_unavailable"


class ExternalRollbackWitnessStatus(str, Enum):
    UNAVAILABLE = "unavailable"
    STRUCTURALLY_VALIDATED = "structurally_validated"
    STALE = "stale"
    INVALID = "invalid"
    INCONSISTENT = "inconsistent"


class TrustAnchorGovernanceRequest(IndependentTrustGovernanceModel):
    schema_version: Literal["trust-anchor-governance-request/v1"] = (
        "trust-anchor-governance-request/v1"
    )
    request_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    authority_id: str = Field(min_length=1, max_length=160)
    anchor_id: str = Field(min_length=1, max_length=160)
    anchor_version: str = Field(min_length=1, max_length=80)
    anchor_generation: int = Field(ge=1)
    anchor_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    scope: PersonalTrustScope
    policy_version: str = Field(min_length=1, max_length=80)
    minimum_source_sequence: int = Field(ge=0)
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=86_400)

    @field_validator("authority_id", "anchor_id", "anchor_version", "policy_version", mode="before")
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


class TrustAnchorGovernanceManifest(IndependentTrustGovernanceModel):
    schema_version: Literal["trust-anchor-governance-manifest/v1"] = (
        "trust-anchor-governance-manifest/v1"
    )
    manifest_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    governance_authority_id: str = Field(min_length=1, max_length=160)
    governance_root_id: str = Field(min_length=1, max_length=160)
    governance_root_version: str = Field(min_length=1, max_length=80)
    governance_root_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    authority_id: str = Field(min_length=1, max_length=160)
    anchor_id: str = Field(min_length=1, max_length=160)
    anchor_version: str = Field(min_length=1, max_length=80)
    anchor_generation: int = Field(ge=1)
    anchor_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    scope: PersonalTrustScope
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=80)
    lifecycle: TrustAnchorLifecycle
    source_sequence: int = Field(gt=0)
    issued_at: datetime
    valid_from: datetime
    expires_at: datetime
    previous_manifest_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replacement_anchor_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replacement_generation: int | None = Field(default=None, ge=2)
    reason_code: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator(
        "governance_authority_id", "governance_root_id", "governance_root_version",
        "authority_id", "anchor_id", "anchor_version", "policy_version", mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("issued_at", "valid_from", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_code", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", normalized) is None:
            raise ValueError("governance reason code must be canonical")
        return normalized

    @model_validator(mode="after")
    def validate_manifest(self) -> "TrustAnchorGovernanceManifest":
        if self.scope_fingerprint != self.scope.fingerprint:
            raise ValueError("governance manifest must bind the exact scope")
        if not self.issued_at <= self.valid_from < self.expires_at:
            raise ValueError("governance manifest validity window is inconsistent")
        if self.anchor_generation == 1 and self.previous_manifest_fingerprint is not None:
            raise ValueError("initial anchor generation cannot name a previous manifest")
        if self.anchor_generation > 1 and self.previous_manifest_fingerprint is None:
            raise ValueError("rotated anchors require previous-manifest lineage")
        replacement = (self.replacement_anchor_fingerprint, self.replacement_generation)
        if self.lifecycle in {TrustAnchorLifecycle.ROTATION_PENDING, TrustAnchorLifecycle.SUPERSEDED}:
            if any(item is None for item in replacement):
                raise ValueError("rotation lifecycle requires complete replacement lineage")
            if self.replacement_generation is not None and self.replacement_generation <= self.anchor_generation:
                raise ValueError("replacement generation must advance")
        elif any(item is not None for item in replacement):
            raise ValueError("only rotation lifecycle may include replacement lineage")
        if self.lifecycle is TrustAnchorLifecycle.ACTIVE and self.reason_code is not None:
            raise ValueError("active governance manifests cannot contain a failure reason")
        if self.lifecycle is not TrustAnchorLifecycle.ACTIVE and self.reason_code is None:
            raise ValueError("non-active governance manifests require a reason")
        return self

    @computed_field(return_type=str)
    @property
    def manifest_fingerprint(self) -> str:
        return sha256(canonical_governance_manifest_payload(self)).hexdigest()


class SignedTrustAnchorGovernanceManifest(IndependentTrustGovernanceModel):
    schema_version: Literal["signed-trust-anchor-governance-manifest/v1"] = (
        "signed-trust-anchor-governance-manifest/v1"
    )
    manifest: TrustAnchorGovernanceManifest
    signature_base64: str = Field(min_length=88, max_length=88)

    @field_validator("signature_base64")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError("governance signature must be strict base64") from error
        if len(decoded) != 64:
            raise ValueError("governance Ed25519 signatures require exactly 64 bytes")
        return value

    @property
    def signature_bytes(self) -> bytes:
        return base64.b64decode(self.signature_base64, validate=True)


class IndependentTrustGovernanceAssessment(IndependentTrustGovernanceModel):
    schema_version: Literal["independent-trust-governance-assessment/v1"] = (
        "independent-trust-governance-assessment/v1"
    )
    status: IndependentTrustGovernanceStatus
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    governance_root_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    manifest_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    anchor_generation: int | None = Field(default=None, ge=1)
    lifecycle: TrustAnchorLifecycle | None = None
    signature_verified: bool = False
    lineage_verified: bool = False
    reason_codes: tuple[str, ...] = ()
    independent_authentication_established: Literal[False] = False
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
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "IndependentTrustGovernanceAssessment":
        if self.status is IndependentTrustGovernanceStatus.VALID_INDEPENDENTLY_AUTHENTICATED:
            raise ValueError("this phase cannot construct independently authenticated governance")
        verified_lineage = (
            self.governance_root_fingerprint,
            self.manifest_fingerprint,
            self.anchor_generation,
            self.lifecycle,
        )
        if self.signature_verified or self.lineage_verified:
            if any(item is None for item in verified_lineage):
                raise ValueError("verified governance requires complete lineage")
        if self.status is IndependentTrustGovernanceStatus.CONFIGURED_UNAUTHENTICATED:
            if not self.signature_verified or not self.lineage_verified or not self.reason_codes:
                raise ValueError("configured governance requires verified lineage and an independence blocker")
        elif not self.reason_codes:
            raise ValueError("non-authoritative governance assessments require reasons")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class ExternalRollbackWitnessRequest(IndependentTrustGovernanceModel):
    schema_version: Literal["external-rollback-witness-request/v1"] = (
        "external-rollback-witness-request/v1"
    )
    request_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    expected_provider_id: str = Field(min_length=1, max_length=160)
    authority_id: str = Field(min_length=1, max_length=160)
    anchor_generation: int = Field(ge=1)
    anchor_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    replay_store_id: str = Field(min_length=32, max_length=64, pattern=r"^[0-9a-f]+$")
    replay_checkpoint_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=86_400)

    @field_validator("expected_provider_id", "authority_id", mode="before")
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


class ExternalRollbackWitnessObservation(IndependentTrustGovernanceModel):
    schema_version: Literal["external-rollback-witness-observation/v1"] = (
        "external-rollback-witness-observation/v1"
    )
    available: bool
    provider_id: str = Field(min_length=1, max_length=160)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    authority_id: str | None = Field(default=None, min_length=1, max_length=160)
    anchor_generation: int | None = Field(default=None, ge=1)
    anchor_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replay_store_id: str | None = Field(
        default=None, min_length=32, max_length=64, pattern=r"^[0-9a-f]+$"
    )
    replay_checkpoint_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    witness_sequence: int | None = Field(default=None, gt=0)
    observed_at: datetime | None = None
    retrieved_at: datetime
    expires_at: datetime | None = None
    reason_codes: tuple[str, ...] = ()
    provider_authenticated: Literal[False] = False
    independent_monotonicity_established: Literal[False] = False

    @field_validator("provider_id", "authority_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "retrieved_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_observation(self) -> "ExternalRollbackWitnessObservation":
        facts = (
            self.authority_id, self.anchor_generation, self.anchor_fingerprint,
            self.replay_store_id, self.replay_checkpoint_fingerprint,
            self.witness_sequence, self.observed_at, self.expires_at,
        )
        if self.available:
            if any(item is None for item in facts) or self.reason_codes:
                raise ValueError("available witness observations require complete facts and no failures")
            assert self.observed_at is not None and self.expires_at is not None
            if not self.observed_at <= self.retrieved_at < self.expires_at:
                raise ValueError("witness chronology is inconsistent")
        elif any(item is not None for item in facts) or not self.reason_codes:
            raise ValueError("unavailable witness observations require failures and no witness facts")
        return self

    @computed_field(return_type=str)
    @property
    def observation_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"observation_fingerprint"}))


class ExternalRollbackWitnessAssessment(IndependentTrustGovernanceModel):
    schema_version: Literal["external-rollback-witness-assessment/v1"] = (
        "external-rollback-witness-assessment/v1"
    )
    status: ExternalRollbackWitnessStatus
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    observation_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reason_codes: tuple[str, ...] = ()
    provider_authenticated: Literal[False] = False
    independent_monotonicity_established: Literal[False] = False
    external_rollback_assurance: Literal[False] = False
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
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "ExternalRollbackWitnessAssessment":
        if self.status is ExternalRollbackWitnessStatus.STRUCTURALLY_VALIDATED:
            if self.observation_fingerprint is None or not self.reason_codes:
                raise ValueError("structural witness validation requires lineage and an external-assurance blocker")
        elif not self.reason_codes:
            raise ValueError("failed witness assessments require reasons")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def canonical_governance_manifest_payload(manifest: TrustAnchorGovernanceManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json", exclude={"manifest_fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trust-governance timestamps require timezone awareness")
    return value.astimezone(UTC)


def _reason_codes(value: object) -> object:
    if not isinstance(value, (tuple, list)):
        return value
    normalized = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
    if any(re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None for item in normalized):
        raise ValueError("reason codes must be canonical bounded identifiers")
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
