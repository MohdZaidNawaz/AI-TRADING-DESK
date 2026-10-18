"""Immutable, non-authorizing personal policy-signing contracts.

The contracts bind a complete controlled-demo policy to a versioned canonical
payload.  They do not establish an independent approver, trust anchor, or
production authorization merely by construction or by signature validity.
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

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.schemas import APIModel
from app.models.sentinel_policy_approval import SentinelPolicySignatureAlgorithm


class PersonalPolicySigningModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PersonalKeyProtection(str, Enum):
    UNAVAILABLE = "unavailable"
    TEST_ONLY = "test_only"
    WINDOWS_DPAPI_CURRENT_USER = "windows_dpapi_current_user"
    WINDOWS_CNG_NON_EXPORTABLE = "windows_cng_non_exportable"


class PersonalKeyProviderAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PersonalTrustState(str, Enum):
    UNCONFIGURED = "unconfigured"
    LOCAL_DECLARATION_ONLY = "local_declaration_only"
    TEST_ONLY = "test_only"


class PersonalRevocationAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PersonalPolicyVerificationStatus(str, Enum):
    TEST_ONLY_VERIFIED = "test_only_verified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    REPLAYED = "replayed"
    UNAVAILABLE = "unavailable"


class PersonalApprovalReplayStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class PersonalKeyProviderStatus(PersonalPolicySigningModel):
    provider_id: str = Field(min_length=1, max_length=160)
    availability: PersonalKeyProviderAvailability
    protection: PersonalKeyProtection
    platform: str = Field(min_length=1, max_length=80)
    reason_codes: tuple[str, ...] = ()
    production_trust_established: Literal[False] = False

    @field_validator("provider_id", "platform", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_status(self) -> "PersonalKeyProviderStatus":
        if self.availability is PersonalKeyProviderAvailability.AVAILABLE:
            if self.protection is PersonalKeyProtection.UNAVAILABLE or self.reason_codes:
                raise ValueError("available key providers require stated protection and no failures")
        elif self.protection is not PersonalKeyProtection.UNAVAILABLE or not self.reason_codes:
            raise ValueError("unavailable key providers require an unavailable protection state and failures")
        return self


class PersonalPublicKeyDescriptor(PersonalPolicySigningModel):
    schema_version: Literal["personal-public-key/v1"] = "personal-public-key/v1"
    key_id: str = Field(min_length=1, max_length=160)
    algorithm: Literal[SentinelPolicySignatureAlgorithm.ED25519] = (
        SentinelPolicySignatureAlgorithm.ED25519
    )
    public_key_pem: str = Field(min_length=80, max_length=8192)
    generation: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime | None = None
    protection: PersonalKeyProtection

    @field_validator("key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("public_key_pem", mode="before")
    @classmethod
    def validate_public_key(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().replace("\r\n", "\n") + "\n"
        if "PRIVATE KEY" in normalized or not (
            normalized.startswith("-----BEGIN PUBLIC KEY-----\n")
            and normalized.endswith("-----END PUBLIC KEY-----\n")
        ):
            raise ValueError("only a public SubjectPublicKeyInfo PEM may be exported")
        return normalized

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_descriptor(self) -> "PersonalPublicKeyDescriptor":
        if self.protection is PersonalKeyProtection.UNAVAILABLE:
            raise ValueError("a public key cannot claim unavailable protection")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("key expiry must follow key creation")
        return self

    @computed_field(return_type=str)
    @property
    def public_key_fingerprint(self) -> str:
        return sha256(self.public_key_pem.encode("ascii")).hexdigest()


class PersonalKeyRotation(PersonalPolicySigningModel):
    previous_key_id: str = Field(min_length=1, max_length=160)
    replacement_key_id: str = Field(min_length=1, max_length=160)
    replacement_generation: int = Field(ge=2)
    rotated_at: datetime
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("previous_key_id", "replacement_key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("rotated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def distinct_keys(self) -> "PersonalKeyRotation":
        if self.previous_key_id == self.replacement_key_id:
            raise ValueError("key rotation requires a distinct replacement key")
        return self


class PersonalKeyRevocation(PersonalPolicySigningModel):
    key_id: str = Field(min_length=1, max_length=160)
    revoked_at: datetime
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("revoked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)


class PersonalKeyRecoveryStatus(PersonalPolicySigningModel):
    key_id: str = Field(min_length=1, max_length=160)
    metadata_available: bool
    signing_capability_available: bool
    requires_external_recovery: bool
    reason_codes: tuple[str, ...] = ()
    private_key_material: Literal[None] = None

    @field_validator("key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_recovery(self) -> "PersonalKeyRecoveryStatus":
        if not self.signing_capability_available and not self.reason_codes:
            raise ValueError("unavailable signing recovery requires a reason")
        return self


class PersonalPolicyApprovalPayload(PersonalPolicySigningModel):
    schema_version: Literal["personal-policy-approval-payload/v1"] = (
        "personal-policy-approval-payload/v1"
    )
    approval_id: str = Field(
        min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    trust_key_id: str = Field(min_length=1, max_length=160)
    issued_at: datetime
    expires_at: datetime
    policy: ControlledDemoExecutionPolicy

    @field_validator("policy_id", "policy_version", "trust_key_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def bind_complete_policy(self) -> "PersonalPolicyApprovalPayload":
        if (
            self.policy_id != self.policy.policy_id
            or self.policy_version != self.policy.policy_version
            or self.policy_fingerprint != self.policy.fingerprint
        ):
            raise ValueError("signed payload must bind the exact policy identity and fingerprint")
        if self.issued_at >= self.expires_at:
            raise ValueError("approval expiry must follow issuance")
        if self.policy.effective_at is not None and self.issued_at < self.policy.effective_at:
            raise ValueError("approval cannot be issued before policy effectiveness")
        if self.policy.expires_at is not None and self.expires_at > self.policy.expires_at:
            raise ValueError("approval cannot outlive the policy")
        return self

    @computed_field(return_type=str)
    @property
    def payload_fingerprint(self) -> str:
        return sha256(canonical_personal_policy_payload(self)).hexdigest()


class PersonalPolicySignatureEnvelope(PersonalPolicySigningModel):
    schema_version: Literal["personal-policy-signature/v1"] = "personal-policy-signature/v1"
    approval_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    key_id: str = Field(min_length=1, max_length=160)
    algorithm: Literal[SentinelPolicySignatureAlgorithm.ED25519] = (
        SentinelPolicySignatureAlgorithm.ED25519
    )
    payload_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    signature_base64: str = Field(min_length=88, max_length=88)

    @field_validator("key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("signature_base64")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError("signature must be strict base64") from error
        if len(decoded) != 64:
            raise ValueError("Ed25519 signatures must contain exactly 64 bytes")
        return value

    @property
    def signature_bytes(self) -> bytes:
        return base64.b64decode(self.signature_base64, validate=True)


class PersonalTrustSnapshot(PersonalPolicySigningModel):
    schema_version: Literal["personal-trust-snapshot/v1"] = "personal-trust-snapshot/v1"
    state: PersonalTrustState
    source_id: str = Field(min_length=1, max_length=160)
    active_keys: tuple[PersonalPublicKeyDescriptor, ...] = ()
    rotations: tuple[PersonalKeyRotation, ...] = ()
    revocations: tuple[PersonalKeyRevocation, ...] = ()
    revocation_availability: PersonalRevocationAvailability
    observed_at: datetime
    retrieved_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)
    reason_codes: tuple[str, ...] = ()
    independently_established: Literal[False] = False

    @field_validator("source_id", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("active_keys")
    @classmethod
    def stable_keys(
        cls, value: tuple[PersonalPublicKeyDescriptor, ...]
    ) -> tuple[PersonalPublicKeyDescriptor, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.key_id))
        if len({item.key_id for item in ordered}) != len(ordered):
            raise ValueError("trust snapshots cannot contain duplicate key IDs")
        return ordered

    @field_validator("rotations")
    @classmethod
    def stable_rotations(cls, value: tuple[PersonalKeyRotation, ...]) -> tuple[PersonalKeyRotation, ...]:
        return tuple(sorted(value, key=lambda item: (item.rotated_at, item.previous_key_id)))

    @field_validator("revocations")
    @classmethod
    def stable_revocations(cls, value: tuple[PersonalKeyRevocation, ...]) -> tuple[PersonalKeyRevocation, ...]:
        ordered = tuple(sorted(value, key=lambda item: (item.revoked_at, item.key_id)))
        if len({item.key_id for item in ordered}) != len(ordered):
            raise ValueError("trust snapshots cannot contain duplicate key revocations")
        return ordered

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PersonalTrustSnapshot":
        if self.retrieved_at < self.observed_at:
            raise ValueError("trust retrieval cannot precede observation")
        if self.state is PersonalTrustState.UNCONFIGURED:
            if self.active_keys or self.rotations or self.revocations or not self.reason_codes:
                raise ValueError("unconfigured trust requires only explicit failure reasons")
        elif not self.active_keys:
            raise ValueError("configured trust requires at least one active public key")
        active_ids = {item.key_id for item in self.active_keys}
        if any(item.created_at > self.observed_at for item in self.active_keys):
            raise ValueError("trust snapshots cannot observe keys before creation")
        if active_ids & {item.key_id for item in self.revocations}:
            raise ValueError("revoked keys cannot remain active")
        rotated_ids = {item.previous_key_id for item in self.rotations}
        if active_ids & rotated_ids:
            raise ValueError("rotated keys cannot remain active")
        if len(rotated_ids) != len(self.rotations):
            raise ValueError("a key generation cannot have multiple rotation successors")
        if any(item.rotated_at > self.observed_at for item in self.rotations):
            raise ValueError("trust snapshots cannot contain future rotations")
        if any(item.revoked_at > self.observed_at for item in self.revocations):
            raise ValueError("trust snapshots cannot contain future revocations")
        if self.revocation_availability is PersonalRevocationAvailability.UNAVAILABLE and not self.reason_codes:
            raise ValueError("unavailable revocation state requires failure reasons")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class PersonalApprovalReplayResult(PersonalPolicySigningModel):
    status: PersonalApprovalReplayStatus
    approval_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    payload_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime
    reason_codes: tuple[str, ...] = ()

    @field_validator("checked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_result(self) -> "PersonalApprovalReplayResult":
        if self.status is PersonalApprovalReplayStatus.ACCEPTED:
            if self.reason_codes:
                raise ValueError("accepted replay checks cannot contain failures")
        elif not self.reason_codes:
            raise ValueError("failed replay checks require reason codes")
        return self


class PersonalPolicyVerificationResult(PersonalPolicySigningModel):
    status: PersonalPolicyVerificationStatus
    approval_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    payload_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    key_id: str = Field(min_length=1, max_length=160)
    verified_at: datetime
    cryptographic_integrity_verified: bool = False
    reason_codes: tuple[str, ...] = ()
    independent_authority_verified: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("verified_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_result(self) -> "PersonalPolicyVerificationResult":
        if self.status is PersonalPolicyVerificationStatus.TEST_ONLY_VERIFIED:
            if not self.cryptographic_integrity_verified or self.reason_codes:
                raise ValueError("test-only verification requires cryptographic integrity and no failures")
        elif not self.reason_codes:
            raise ValueError("failed personal policy verification requires reason codes")
        return self


def canonical_personal_policy_payload(payload: PersonalPolicyApprovalPayload) -> bytes:
    """Return versioned RFC-8259-style canonical UTF-8 bytes for signing."""

    data = payload.model_dump(mode="json", exclude={"payload_fingerprint"})
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _reason_codes(value: object) -> object:
    if not isinstance(value, (tuple, list)):
        return value
    normalized = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
    if any(re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None for item in normalized):
        raise ValueError("reason codes must be canonical bounded identifiers")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("personal policy signing timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
