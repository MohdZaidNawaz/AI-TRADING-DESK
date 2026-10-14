"""Formal, non-authorizing governance contracts for Sentinel policy approval.

A record describes an organizational decision and its required trust mechanism.
It is not proof that the decision was legitimately made.  Only an independently
verified record resolved from an authoritative source may establish authority;
the repository currently has no such source or trust anchor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalMechanism,
    SentinelPolicySignatureAlgorithm,
)


class SentinelPolicyGovernanceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SentinelPolicyGovernanceTrustLevel(str, Enum):
    UNCONFIGURED = "unconfigured"
    LOCAL_DECLARATION_ONLY = "local_declaration_only"
    TEST_ONLY = "test_only"
    INDEPENDENTLY_VERIFIED = "independently_verified"


class SentinelPolicyGovernanceLifecycle(str, Enum):
    UNCONFIGURED = "unconfigured"
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class SentinelPolicyGovernanceCapability(str, Enum):
    APPROVE_POLICY = "approve_policy"
    REVOKE_POLICY = "revoke_policy"
    SUPERSEDE_POLICY = "supersede_policy"


class SentinelPolicyTrustAnchorType(str, Enum):
    ED25519_PUBLIC_KEY = "ed25519_public_key"
    EXTERNAL_GOVERNANCE_SERVICE = "external_governance_service"


class SentinelPolicyGovernanceSourceStatus(str, Enum):
    AVAILABLE = "available"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class SentinelPolicyGovernanceScope(SentinelPolicyGovernanceModel):
    policy_ids: tuple[str, ...] = Field(min_length=1)
    policy_versions: tuple[str, ...] = Field(min_length=1)
    execution_modes: tuple[str, ...] = Field(min_length=1)
    account_ids: tuple[str, ...] = Field(min_length=1)
    canonical_symbols: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "policy_ids", "policy_versions", "execution_modes", "account_ids", "canonical_symbols",
        mode="before",
    )
    @classmethod
    def normalize_scope(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        return tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))


class SentinelPolicyTrustAnchorDescriptor(SentinelPolicyGovernanceModel):
    """Public trust metadata only; never private signing material."""

    anchor_type: SentinelPolicyTrustAnchorType
    anchor_id: str = Field(min_length=1, max_length=160)
    algorithm: SentinelPolicySignatureAlgorithm | None = None
    public_identity_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    provenance_reference: str = Field(min_length=1, max_length=240)
    independently_established_at: datetime
    expires_at: datetime | None = None

    @field_validator("anchor_id", "provenance_reference", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if any(marker in normalized.lower() for marker in ("password", "private_key", "secret=", "token=", "credential=")):
            raise ValueError("trust-anchor provenance cannot contain sensitive material")
        return normalized

    @field_validator("independently_established_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_anchor(self) -> "SentinelPolicyTrustAnchorDescriptor":
        if self.anchor_type is SentinelPolicyTrustAnchorType.ED25519_PUBLIC_KEY:
            if self.algorithm is not SentinelPolicySignatureAlgorithm.ED25519:
                raise ValueError("Ed25519 trust anchors require the Ed25519 algorithm")
        elif self.algorithm is not None:
            raise ValueError("external governance anchors cannot claim a local signature algorithm")
        if self.expires_at is not None and self.expires_at <= self.independently_established_at:
            raise ValueError("trust-anchor expiry must follow independent establishment")
        return self


class SentinelPolicyGovernanceRecord(SentinelPolicyGovernanceModel):
    """A versioned governance declaration, not authority merely by construction."""

    schema_version: Literal["sentinel-policy-governance/v1"] = "sentinel-policy-governance/v1"
    governance_contract_version: Literal["1"] = "1"
    trust_level: SentinelPolicyGovernanceTrustLevel
    lifecycle: SentinelPolicyGovernanceLifecycle
    lifecycle_version: int = Field(ge=1)
    source_id: str = Field(min_length=1, max_length=160)
    source_version: str = Field(min_length=1, max_length=80)
    approver_id: str | None = Field(default=None, min_length=1, max_length=160)
    organizational_role: str | None = Field(default=None, min_length=1, max_length=160)
    capabilities: tuple[SentinelPolicyGovernanceCapability, ...] = ()
    scope: SentinelPolicyGovernanceScope | None = None
    required_approval_mechanism: SentinelPolicyApprovalMechanism | None = None
    trust_anchor: SentinelPolicyTrustAnchorDescriptor | None = None
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    registered_at: datetime
    lifecycle_changed_at: datetime
    audit_requirements: tuple[str, ...] = ()
    unconfigured_reason: str | None = Field(default=None, min_length=1, max_length=240)
    revoked_reason: str | None = Field(default=None, min_length=1, max_length=240)
    superseded_by_record_id: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("source_id", "source_version", "approver_id", "organizational_role", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("valid_from", "expires_at", "registered_at", "lifecycle_changed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("capabilities")
    @classmethod
    def stable_capabilities(
        cls, value: tuple[SentinelPolicyGovernanceCapability, ...]
    ) -> tuple[SentinelPolicyGovernanceCapability, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("audit_requirements", mode="before")
    @classmethod
    def stable_audit_requirements(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        if any(
            len(item) > 160
            or "=" in item
            or any(marker in item.lower() for marker in ("password", "private_key", "secret", "token", "credential"))
            for item in normalized
        ):
            raise ValueError("governance audit requirements must be safe identifiers")
        return normalized

    @model_validator(mode="after")
    def validate_governance(self) -> "SentinelPolicyGovernanceRecord":
        if self.lifecycle_changed_at < self.registered_at:
            raise ValueError("governance lifecycle change cannot precede registration")
        configured_fields = (
            self.approver_id,
            self.organizational_role,
            self.scope,
            self.required_approval_mechanism,
            self.trust_anchor,
            self.valid_from,
            self.expires_at,
        )
        if self.trust_level is SentinelPolicyGovernanceTrustLevel.UNCONFIGURED:
            if self.lifecycle is not SentinelPolicyGovernanceLifecycle.UNCONFIGURED:
                raise ValueError("unconfigured governance requires unconfigured lifecycle")
            if any(value is not None for value in configured_fields) or self.capabilities or self.audit_requirements:
                raise ValueError("unconfigured governance cannot name an approver or authority")
            if self.unconfigured_reason is None or self.revoked_reason is not None or self.superseded_by_record_id is not None:
                raise ValueError("unconfigured governance requires only an explicit missing-dependency reason")
            return self

        if self.lifecycle is SentinelPolicyGovernanceLifecycle.UNCONFIGURED:
            raise ValueError("configured governance cannot use unconfigured lifecycle")
        if any(value is None for value in configured_fields[:4]) or self.valid_from is None or self.expires_at is None:
            raise ValueError("configured governance requires approver, role, scope, mechanism, and validity")
        if self.valid_from >= self.expires_at:
            raise ValueError("governance expiry must follow its effective time")
        required_capabilities = {
            SentinelPolicyGovernanceCapability.APPROVE_POLICY,
            SentinelPolicyGovernanceCapability.REVOKE_POLICY,
        }
        if not required_capabilities.issubset(set(self.capabilities)):
            raise ValueError("configured governance requires approval and revocation capabilities")
        if not self.audit_requirements:
            raise ValueError("configured governance requires explicit audit responsibilities")
        if self.unconfigured_reason is not None:
            raise ValueError("configured governance cannot retain an unconfigured reason")
        if self.trust_level is SentinelPolicyGovernanceTrustLevel.INDEPENDENTLY_VERIFIED:
            if self.trust_anchor is None:
                raise ValueError("independent governance requires a verified trust anchor descriptor")
            if self.required_approval_mechanism is SentinelPolicyApprovalMechanism.TEST_FIXTURE:
                raise ValueError("independent governance cannot use a test fixture")
        elif self.trust_anchor is not None:
            raise ValueError("non-authoritative governance cannot advertise a trust anchor")
        if self.trust_level is SentinelPolicyGovernanceTrustLevel.TEST_ONLY:
            if self.required_approval_mechanism is not SentinelPolicyApprovalMechanism.TEST_FIXTURE:
                raise ValueError("test-only governance requires the test-fixture mechanism")
        elif self.required_approval_mechanism is SentinelPolicyApprovalMechanism.TEST_FIXTURE:
            raise ValueError("test fixtures cannot be used outside test-only governance")
        if self.lifecycle is SentinelPolicyGovernanceLifecycle.ACTIVE:
            if self.lifecycle_version != 1 or self.revoked_reason is not None or self.superseded_by_record_id is not None:
                raise ValueError("active governance cannot contain terminal metadata")
        elif self.lifecycle is SentinelPolicyGovernanceLifecycle.REVOKED:
            if self.lifecycle_version < 2 or self.revoked_reason is None or self.superseded_by_record_id is not None:
                raise ValueError("revoked governance requires a later version and reason")
        elif self.lifecycle_version < 2 or self.superseded_by_record_id is None or self.revoked_reason is not None:
            raise ValueError("superseded governance requires a later version and replacement")
        return self

    @computed_field(return_type=str)
    @property
    def governance_record_id(self) -> str:
        return _fingerprint(
            self.model_dump(
                mode="json",
                exclude={"governance_record_id", "record_fingerprint", "lifecycle", "lifecycle_version", "lifecycle_changed_at", "revoked_reason", "superseded_by_record_id"},
            )
        )

    @computed_field(return_type=str)
    @property
    def record_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"record_fingerprint"}))


class SentinelPolicyGovernanceSourceResult(SentinelPolicyGovernanceModel):
    status: SentinelPolicyGovernanceSourceStatus
    source_id: str = Field(min_length=1, max_length=160)
    requested_policy_id: str = Field(min_length=1, max_length=160)
    requested_policy_version: str = Field(min_length=1, max_length=80)
    resolved_at: datetime
    record: SentinelPolicyGovernanceRecord | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator("requested_policy_id", "requested_policy_version", mode="before")
    @classmethod
    def normalize_request(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("resolved_at")
    @classmethod
    def normalize_resolved_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_result(self) -> "SentinelPolicyGovernanceSourceResult":
        if self.status is SentinelPolicyGovernanceSourceStatus.AVAILABLE:
            if self.record is None or self.reason_codes:
                raise ValueError("available governance result requires one record and no failures")
            scope = self.record.scope
            if scope is None or (
                self.requested_policy_id not in scope.policy_ids
                or self.requested_policy_version not in scope.policy_versions
            ):
                raise ValueError("governance-source result must bind the exact policy request")
        elif self.record is not None or not self.reason_codes:
            raise ValueError("unavailable governance result requires failures and no record")
        return self


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy governance timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
