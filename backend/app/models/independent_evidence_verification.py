"""Immutable, non-authorizing Phase 22 evidence-verification contracts."""

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

from app.models.dependency_acceptance import (
    DependencyAcceptanceContext,
    ExternalDependencyEvidenceEnvelope,
    ExternalDependencyId,
    IntegrationEnvironment,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.models.schemas import APIModel


_FINGERPRINT = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[A-Z0-9][A-Z0-9._:/-]{0,127}$"
_REASON = r"^[A-Z][A-Z0-9_]{0,119}$"


class EvidenceVerificationModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceSignatureAlgorithm(str, Enum):
    ED25519 = "ED25519"


class ConfiguredKeyLifecycle(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    UNKNOWN = "unknown"


class StructuralVerificationStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class SignatureVerificationStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    NOT_CHECKED = "not_checked"


class IssuerAuthenticationStatus(str, Enum):
    CONFIGURED_SYNTHETIC_KEY = "configured_synthetic_key"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


class BindingVerificationStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    NOT_CHECKED = "not_checked"


class LifecycleVerificationStatus(str, Enum):
    CLAIMED_ACTIVE_UNVERIFIED = "claimed_active_unverified"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class FreshnessVerificationStatus(str, Enum):
    FRESH = "fresh"
    FUTURE_DATED = "future_dated"
    EXPIRED = "expired"
    STALE = "stale"
    NOT_CHECKED = "not_checked"


class ReplayEvaluationStatus(str, Enum):
    NOT_SEEN_IN_SUPPLIED_SET = "not_seen_in_supplied_set"
    DUPLICATE_IN_SUPPLIED_SET = "duplicate_in_supplied_set"
    NOT_CHECKED = "not_checked"


class EvidenceVerificationDisposition(str, Enum):
    CONFIGURED_KEY_VERIFIED_NON_AUTHORITATIVE = "configured_key_verified_non_authoritative"
    STRUCTURALLY_INVALID = "structurally_invalid"
    SIGNATURE_INVALID = "signature_invalid"
    BINDING_INVALID = "binding_invalid"
    STALE = "stale"
    REVOKED = "revoked"
    DUPLICATE = "duplicate"
    UNAVAILABLE = "unavailable"


class EvidenceVerificationReason(str, Enum):
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    EVIDENCE_STRUCTURE_INVALID = "EVIDENCE_STRUCTURE_INVALID"
    VERIFIER_CONFIGURATION_UNAVAILABLE = "VERIFIER_CONFIGURATION_UNAVAILABLE"
    VERIFICATION_POLICY_MISMATCH = "VERIFICATION_POLICY_MISMATCH"
    SIGNATURE_ALGORITHM_UNSUPPORTED = "SIGNATURE_ALGORITHM_UNSUPPORTED"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    TRUST_ROOT_NOT_CURRENT = "TRUST_ROOT_NOT_CURRENT"
    TRUST_ROOT_REVOKED = "TRUST_ROOT_REVOKED"
    TRUST_ROOT_SUPERSEDED = "TRUST_ROOT_SUPERSEDED"
    TRUST_ROOT_COMPROMISED = "TRUST_ROOT_COMPROMISED"
    TRUST_ROOT_LIFECYCLE_UNKNOWN = "TRUST_ROOT_LIFECYCLE_UNKNOWN"
    ISSUER_IDENTITY_MISMATCH = "ISSUER_IDENTITY_MISMATCH"
    KEY_IDENTITY_MISMATCH = "KEY_IDENTITY_MISMATCH"
    KEY_GENERATION_MISMATCH = "KEY_GENERATION_MISMATCH"
    ROOT_FINGERPRINT_MISMATCH = "ROOT_FINGERPRINT_MISMATCH"
    ROOT_VERSION_MISMATCH = "ROOT_VERSION_MISMATCH"
    DEPENDENCY_NOT_PERMITTED = "DEPENDENCY_NOT_PERMITTED"
    ENVIRONMENT_NOT_PERMITTED = "ENVIRONMENT_NOT_PERMITTED"
    SCOPE_NOT_PERMITTED = "SCOPE_NOT_PERMITTED"
    SUBJECT_BINDING_MISMATCH = "SUBJECT_BINDING_MISMATCH"
    REQUEST_BINDING_MISMATCH = "REQUEST_BINDING_MISMATCH"
    CHALLENGE_BINDING_MISMATCH = "CHALLENGE_BINDING_MISMATCH"
    SCOPE_BINDING_MISMATCH = "SCOPE_BINDING_MISMATCH"
    ENVIRONMENT_BINDING_MISMATCH = "ENVIRONMENT_BINDING_MISMATCH"
    POLICY_BINDING_MISMATCH = "POLICY_BINDING_MISMATCH"
    EVALUATION_TIME_MISMATCH = "EVALUATION_TIME_MISMATCH"
    COMMIT_BINDING_MISSING = "COMMIT_BINDING_MISSING"
    COMMIT_BINDING_MISMATCH = "COMMIT_BINDING_MISMATCH"
    EVIDENCE_TYPE_MISMATCH = "EVIDENCE_TYPE_MISMATCH"
    EVIDENCE_FUTURE_DATED = "EVIDENCE_FUTURE_DATED"
    EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_REVOKED = "EVIDENCE_REVOKED"
    EVIDENCE_SUPERSEDED = "EVIDENCE_SUPERSEDED"
    EVIDENCE_COMPROMISED = "EVIDENCE_COMPROMISED"
    EVIDENCE_LIFECYCLE_UNKNOWN = "EVIDENCE_LIFECYCLE_UNKNOWN"
    EMBEDDED_PUBLIC_KEY_NOT_TRUSTED = "EMBEDDED_PUBLIC_KEY_NOT_TRUSTED"
    CALLER_VERIFICATION_CLAIM_IGNORED = "CALLER_VERIFICATION_CLAIM_IGNORED"
    SYNTHETIC_TRUST_ROOT_NON_AUTHORITATIVE = "SYNTHETIC_TRUST_ROOT_NON_AUTHORITATIVE"
    INDEPENDENT_LIFECYCLE_VERIFICATION_UNAVAILABLE = (
        "INDEPENDENT_LIFECYCLE_VERIFICATION_UNAVAILABLE"
    )
    EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE = "EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE"
    EVIDENCE_DUPLICATE_IN_SUPPLIED_SET = "EVIDENCE_DUPLICATE_IN_SUPPLIED_SET"


class EvidenceVerificationPolicy(EvidenceVerificationModel):
    schema_version: Literal["evidence-verification-policy/v1"] = "evidence-verification-policy/v1"
    policy_id: Literal["FRIDAY_PHASE22_OFFLINE_EVIDENCE_VERIFICATION"] = (
        "FRIDAY_PHASE22_OFFLINE_EVIDENCE_VERIFICATION"
    )
    policy_version: Literal["phase22-independent-evidence-verification/v1"] = (
        "phase22-independent-evidence-verification/v1"
    )
    algorithms: tuple[EvidenceSignatureAlgorithm, ...] = (EvidenceSignatureAlgorithm.ED25519,)
    independent_verification_available: Literal[False] = False
    durable_replay_protection_available: Literal[False] = False

    @model_validator(mode="after")
    def exact_algorithm_policy(self) -> "EvidenceVerificationPolicy":
        if self.algorithms != (EvidenceSignatureAlgorithm.ED25519,):
            raise ValueError("Phase 22 supports only the canonical Ed25519 policy")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class EvidenceTrustConfiguration(EvidenceVerificationModel):
    """Explicit synthetic verification material; never ambient production trust."""

    schema_version: Literal["evidence-trust-configuration/v1"] = (
        "evidence-trust-configuration/v1"
    )
    configuration_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    issuer_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    key_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    key_generation: int = Field(ge=1)
    algorithm: Literal[EvidenceSignatureAlgorithm.ED25519] = EvidenceSignatureAlgorithm.ED25519
    trust_anchor: ConfiguredEd25519TrustAnchor
    lifecycle: ConfiguredKeyLifecycle
    permitted_dependencies: tuple[ExternalDependencyId, ...] = Field(min_length=1)
    permitted_environments: tuple[IntegrationEnvironment, ...] = Field(min_length=1)
    permitted_scope_fingerprints: tuple[str, ...] = Field(min_length=1)
    synthetic_test_only: Literal[True] = True
    independently_trusted: Literal[False] = False
    lifecycle_independently_verified: Literal[False] = False

    @field_validator("configuration_id", "issuer_id", "key_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("permitted_dependencies", "permitted_environments")
    @classmethod
    def stable_enums(cls, value: tuple[Enum, ...]) -> tuple[Enum, ...]:
        if len(value) != len(set(value)):
            raise ValueError("trust configuration permissions must be unique")
        return tuple(sorted(value, key=lambda item: item.value))

    @field_validator("permitted_scope_fingerprints")
    @classmethod
    def stable_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if len(normalized) != len(value) or any(re.fullmatch(_FINGERPRINT, item) is None for item in normalized):
            raise ValueError("trust configuration scopes require unique SHA-256 fingerprints")
        return normalized

    @model_validator(mode="after")
    def coherent_configuration(self) -> "EvidenceTrustConfiguration":
        if self.issuer_id != self.trust_anchor.authority_id:
            raise ValueError("configured issuer must match the trust-anchor authority")
        if self.key_id != self.trust_anchor.anchor_id:
            raise ValueError("configured key must match the trust-anchor identity")
        if IntegrationEnvironment.PRODUCTION in self.permitted_environments:
            raise ValueError("Phase 22 synthetic trust cannot permit production")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class SignedDependencyEvidence(EvidenceVerificationModel):
    schema_version: Literal["signed-dependency-evidence/v1"] = "signed-dependency-evidence/v1"
    evidence: ExternalDependencyEvidenceEnvelope
    algorithm: Literal[EvidenceSignatureAlgorithm.ED25519] = EvidenceSignatureAlgorithm.ED25519
    key_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    key_generation: int = Field(ge=1)
    verification_root_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Z0-9][A-Z0-9._:-]{0,79}$"
    )
    verification_root_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    verification_policy_version: Literal[
        "phase22-independent-evidence-verification/v1"
    ] = "phase22-independent-evidence-verification/v1"
    signature_base64: str = Field(min_length=88, max_length=88)
    presented_public_key_pem: str | None = Field(default=None, min_length=80, max_length=8192)

    @field_validator("key_id", "verification_root_version", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("signature_base64")
    @classmethod
    def valid_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError("dependency evidence signature must be strict base64") from error
        if len(decoded) != 64:
            raise ValueError("dependency evidence requires an Ed25519 signature")
        return value

    @field_validator("presented_public_key_pem")
    @classmethod
    def valid_untrusted_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("\r\n", "\n") + "\n"
        if "PRIVATE KEY" in normalized:
            raise ValueError("submitted evidence cannot contain private key material")
        try:
            key = serialization.load_pem_public_key(normalized.encode("ascii"))
        except Exception as error:
            raise ValueError("presented public key is malformed") from error
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("presented public key must be Ed25519")
        return normalized

    @property
    def signature_bytes(self) -> bytes:
        return base64.b64decode(self.signature_base64, validate=True)

    @computed_field(return_type=str)
    @property
    def signed_payload_fingerprint(self) -> str:
        return sha256(canonical_dependency_evidence_payload(self)).hexdigest()


class EvidenceVerificationRequest(EvidenceVerificationModel):
    schema_version: Literal["evidence-verification-request/v1"] = (
        "evidence-verification-request/v1"
    )
    context: DependencyAcceptanceContext
    signed_evidence: SignedDependencyEvidence | None = None
    policy_version: Literal["phase22-independent-evidence-verification/v1"] = (
        "phase22-independent-evidence-verification/v1"
    )
    previously_seen_evidence_fingerprints: tuple[str, ...] = ()

    @field_validator("previously_seen_evidence_fingerprints")
    @classmethod
    def stable_seen(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if len(normalized) != len(value) or any(re.fullmatch(_FINGERPRINT, item) is None for item in normalized):
            raise ValueError("seen evidence requires unique SHA-256 fingerprints")
        return normalized

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class EvidenceVerificationReport(EvidenceVerificationModel):
    schema_version: Literal["evidence-verification-report/v1"] = (
        "evidence-verification-report/v1"
    )
    disposition: EvidenceVerificationDisposition
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    policy_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    evaluated_at: datetime
    evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    signed_payload_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    configured_root_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    structural_status: StructuralVerificationStatus
    signature_status: SignatureVerificationStatus
    issuer_status: IssuerAuthenticationStatus
    binding_status: BindingVerificationStatus
    lifecycle_status: LifecycleVerificationStatus
    freshness_status: FreshnessVerificationStatus
    replay_status: ReplayEvaluationStatus
    reason_codes: tuple[EvidenceVerificationReason, ...] = Field(min_length=1)
    independent_verification_available: Literal[False] = False
    independently_verified: Literal[False] = False
    operationally_accepted: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False
    authorizes_execution: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes")
    @classmethod
    def stable_reasons(
        cls, value: tuple[EvidenceVerificationReason, ...]
    ) -> tuple[EvidenceVerificationReason, ...]:
        if len(value) != len(set(value)):
            raise ValueError("verification reasons must be unique")
        return tuple(sorted(value, key=lambda item: item.value))

    @model_validator(mode="after")
    def coherent_report(self) -> "EvidenceVerificationReport":
        if (
            self.disposition
            is EvidenceVerificationDisposition.CONFIGURED_KEY_VERIFIED_NON_AUTHORITATIVE
        ):
            if not (
                self.structural_status is StructuralVerificationStatus.VALID
                and self.signature_status is SignatureVerificationStatus.VALID
                and self.issuer_status
                is IssuerAuthenticationStatus.CONFIGURED_SYNTHETIC_KEY
                and self.binding_status is BindingVerificationStatus.VALID
                and self.freshness_status is FreshnessVerificationStatus.FRESH
            ):
                raise ValueError("configured-key verification requires all local checks")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def canonical_dependency_evidence_payload(envelope: SignedDependencyEvidence) -> bytes:
    evidence = envelope.evidence
    value = {
        "domain": "FRIDAY_PHASE22_DEPENDENCY_EVIDENCE_SIGNATURE_V1",
        "algorithm": envelope.algorithm.value,
        "key_id": envelope.key_id,
        "key_generation": envelope.key_generation,
        "verification_root_version": envelope.verification_root_version,
        "verification_root_fingerprint": envelope.verification_root_fingerprint,
        "verification_policy_version": envelope.verification_policy_version,
        "issuer_id": evidence.issuer_id,
        "dependency_id": evidence.dependency_id.value,
        "evidence_type": evidence.evidence_type.value,
        "evidence_fingerprint": evidence.envelope_fingerprint,
        "evidence": evidence.model_dump(
            mode="json",
            exclude={"envelope_fingerprint"},
            exclude_computed_fields=True,
        ),
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence verification timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
