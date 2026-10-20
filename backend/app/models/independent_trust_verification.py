"""Cryptographic, provider-neutral verification contracts for key-status evidence.

These immutable models can prove that a statement was signed by the private key
corresponding to an explicitly configured Ed25519 public key.  They deliberately
cannot prove that the configured key is independently governed, authorize a
trade, or substitute for externally durable replay/rollback protection.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.personal_trust_bootstrap import (
    PersonalKeyAuthorityRequest,
    PersonalKeyAuthorityState,
    PersonalTrustScope,
)
from app.models.schemas import APIModel


class IndependentTrustVerificationModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IndependentTrustVerificationStatus(str, Enum):
    CRYPTOGRAPHICALLY_VERIFIED = "cryptographically_verified"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    RECOVERY_REQUIRED = "recovery_required"
    REJECTED = "rejected"
    STALE = "stale"
    REPLAYED = "replayed"
    ROLLBACK = "rollback"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class IndependentReplayStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    ROLLBACK = "rollback"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class ConfiguredEd25519TrustAnchor(IndependentTrustVerificationModel):
    """Public verification material configured outside an authority response.

    Construction proves only that the public key is well formed.  It does not
    prove that an operator independently established or reviewed the key.
    """

    schema_version: Literal["configured-ed25519-trust-anchor/v1"] = (
        "configured-ed25519-trust-anchor/v1"
    )
    authority_id: str = Field(min_length=1, max_length=160)
    anchor_id: str = Field(min_length=1, max_length=160)
    anchor_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
    )
    public_key_pem: str = Field(min_length=80, max_length=8192)
    valid_from: datetime
    expires_at: datetime | None = None
    provenance_reference: str = Field(min_length=1, max_length=240)
    independently_trusted: Literal[False] = False

    @field_validator("authority_id", "anchor_id", "anchor_version", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
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
            raise ValueError("trust anchors require a public SubjectPublicKeyInfo PEM")
        try:
            key = serialization.load_pem_public_key(normalized.encode("ascii"))
        except Exception as error:
            raise ValueError("trust-anchor public key is malformed") from error
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("trust anchors require an Ed25519 public key")
        return normalized

    @field_validator("valid_from", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("provenance_reference", mode="before")
    @classmethod
    def safe_reference(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized or _SENSITIVE.search(normalized):
            raise ValueError("trust-anchor provenance must be safe non-secret text")
        return normalized

    @model_validator(mode="after")
    def validate_window(self) -> "ConfiguredEd25519TrustAnchor":
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("trust-anchor expiry must follow validity start")
        return self

    @computed_field(return_type=str)
    @property
    def public_key_fingerprint(self) -> str:
        key = serialization.load_pem_public_key(self.public_key_pem.encode("ascii"))
        assert isinstance(key, Ed25519PublicKey)
        der = key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return sha256(der).hexdigest()


class IndependentKeyStatusStatement(IndependentTrustVerificationModel):
    schema_version: Literal["independent-key-status-statement/v1"] = (
        "independent-key-status-statement/v1"
    )
    statement_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    authority_id: str = Field(min_length=1, max_length=160)
    anchor_id: str = Field(min_length=1, max_length=160)
    anchor_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
    )
    anchor_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    key_id: str = Field(min_length=1, max_length=160)
    public_key_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    key_generation: int = Field(ge=1)
    scope: PersonalTrustScope
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    key_state: PersonalKeyAuthorityState
    source_sequence: int = Field(gt=0)
    observed_at: datetime
    issued_at: datetime
    expires_at: datetime
    replacement_key_id: str | None = Field(default=None, min_length=1, max_length=160)
    replacement_key_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replacement_generation: int | None = Field(default=None, ge=2)

    @field_validator(
        "authority_id", "anchor_id", "anchor_version", "key_id", "replacement_key_id",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "issued_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_statement(self) -> "IndependentKeyStatusStatement":
        if self.scope_fingerprint != self.scope.fingerprint:
            raise ValueError("signed statement must bind the exact scope")
        if not self.observed_at <= self.issued_at < self.expires_at:
            raise ValueError("signed statement timestamps are inconsistent")
        replacement = (
            self.replacement_key_id,
            self.replacement_key_fingerprint,
            self.replacement_generation,
        )
        if self.key_state is PersonalKeyAuthorityState.SUPERSEDED:
            if any(value is None for value in replacement):
                raise ValueError("superseded statements require complete replacement lineage")
            if self.replacement_key_id == self.key_id:
                raise ValueError("replacement key identity must be distinct")
            if self.replacement_generation is not None and self.replacement_generation <= self.key_generation:
                raise ValueError("replacement generation must advance")
        elif any(value is not None for value in replacement):
            raise ValueError("only superseded statements may contain replacement lineage")
        return self

    @computed_field(return_type=str)
    @property
    def statement_fingerprint(self) -> str:
        return sha256(canonical_independent_key_status_payload(self)).hexdigest()


class SignedIndependentKeyStatusStatement(IndependentTrustVerificationModel):
    schema_version: Literal["signed-independent-key-status/v1"] = (
        "signed-independent-key-status/v1"
    )
    statement: IndependentKeyStatusStatement
    signature_base64: str = Field(min_length=88, max_length=88)

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

    @computed_field(return_type=str)
    @property
    def envelope_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"envelope_fingerprint"}))


class IndependentReplayBinding(IndependentTrustVerificationModel):
    authority_id: str = Field(min_length=1, max_length=160)
    anchor_id: str = Field(min_length=1, max_length=160)
    anchor_version: str = Field(min_length=1, max_length=80)
    anchor_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    key_id: str = Field(min_length=1, max_length=160)
    key_generation: int = Field(ge=1)
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    source_sequence: int = Field(gt=0)
    statement_id: str = Field(min_length=32, max_length=160)
    statement_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("authority_id", "anchor_id", "anchor_version", "key_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @computed_field(return_type=str)
    @property
    def binding_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"binding_fingerprint"}))


class IndependentReplayResult(IndependentTrustVerificationModel):
    status: IndependentReplayStatus
    binding_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime
    reason_codes: tuple[str, ...] = ()
    externally_durable: Literal[False] = False

    @field_validator("checked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_result(self) -> "IndependentReplayResult":
        if self.status is IndependentReplayStatus.ACCEPTED:
            if self.reason_codes:
                raise ValueError("accepted replay checks cannot contain failures")
        elif not self.reason_codes:
            raise ValueError("failed replay checks require reasons")
        return self


class IndependentTrustVerificationResult(IndependentTrustVerificationModel):
    status: IndependentTrustVerificationStatus
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    anchor_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    statement_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_state: PersonalKeyAuthorityState | None = None
    signature_verified: bool = False
    replay_binding_verified: bool = False
    reason_codes: tuple[str, ...] = ()
    independently_authoritative: Literal[False] = False
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
    def validate_result(self) -> "IndependentTrustVerificationResult":
        verified_states = {
            IndependentTrustVerificationStatus.CRYPTOGRAPHICALLY_VERIFIED,
            IndependentTrustVerificationStatus.REVOKED,
            IndependentTrustVerificationStatus.SUPERSEDED,
            IndependentTrustVerificationStatus.COMPROMISED,
            IndependentTrustVerificationStatus.RECOVERY_REQUIRED,
        }
        if self.status in verified_states:
            if not self.signature_verified or not self.replay_binding_verified:
                raise ValueError("verified statements require signature and replay binding")
            if self.anchor_fingerprint is None or self.statement_fingerprint is None or self.key_state is None:
                raise ValueError("verified statements require complete lineage")
            if self.status is IndependentTrustVerificationStatus.CRYPTOGRAPHICALLY_VERIFIED:
                if self.key_state is not PersonalKeyAuthorityState.ACTIVE or self.reason_codes:
                    raise ValueError("active verification requires an active key and no failures")
            elif not self.reason_codes:
                raise ValueError("terminal authority states require reason codes")
        elif not self.reason_codes:
            raise ValueError("failed trust verification requires reason codes")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def canonical_independent_key_status_payload(statement: IndependentKeyStatusStatement) -> bytes:
    return json.dumps(
        statement.model_dump(mode="json", exclude={"statement_fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def replay_binding_for(statement: IndependentKeyStatusStatement) -> IndependentReplayBinding:
    return IndependentReplayBinding(
        authority_id=statement.authority_id,
        anchor_id=statement.anchor_id,
        anchor_version=statement.anchor_version,
        anchor_fingerprint=statement.anchor_fingerprint,
        key_id=statement.key_id,
        key_generation=statement.key_generation,
        scope_fingerprint=statement.scope_fingerprint,
        source_sequence=statement.source_sequence,
        statement_id=statement.statement_id,
        statement_fingerprint=statement.statement_fingerprint,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trust-verification timestamps require timezone awareness")
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


_SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|password|secret|token|credential|private[_-]?key)\s*[:=]"
)
