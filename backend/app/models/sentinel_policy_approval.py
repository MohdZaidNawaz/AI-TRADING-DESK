"""Immutable claims and verification results for Sentinel policy approval.

These contracts separate a deterministic approval *claim* from proof that an
independent authority actually approved the policy.  Constructing an artifact
never establishes authority; only a configured verifier backed by a legitimate
trust anchor may return an authoritative result.  No such trust anchor is
configured in this repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class SentinelPolicyApprovalModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SentinelPolicyApprovalMechanism(str, Enum):
    TEST_FIXTURE = "test_fixture"
    DETACHED_SIGNATURE = "detached_signature"
    EXTERNAL_GOVERNANCE_ATTESTATION = "external_governance_attestation"


class SentinelPolicySignatureAlgorithm(str, Enum):
    ED25519 = "ed25519"


class SentinelPolicyApprovalLifecycle(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class SentinelPolicyApprovalVerificationStatus(str, Enum):
    AUTHORITATIVE = "authoritative"
    TEST_ONLY_VERIFIED = "test_only_verified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class SentinelPolicyApprovalSourceStatus(str, Enum):
    AVAILABLE = "available"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class SentinelPolicyApprovalScope(SentinelPolicyApprovalModel):
    """Exact scope for which a policy approval is claimed."""

    account_ids: tuple[str, ...] = Field(min_length=1)
    execution_modes: tuple[str, ...] = Field(min_length=1)
    canonical_symbols: tuple[str, ...] = Field(min_length=1)
    risk_currencies: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "account_ids", "execution_modes", "canonical_symbols", "risk_currencies",
        mode="before",
    )
    @classmethod
    def normalize_scope(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        normalized = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
        return normalized


class SentinelPolicyApprovalArtifact(SentinelPolicyApprovalModel):
    """Version-bound approval claim; never authority merely by construction."""

    schema_version: Literal["sentinel-policy-approval/v1"] = "sentinel-policy-approval/v1"
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    approver_id: str = Field(min_length=1, max_length=160)
    authority_scope: Literal["sentinel_portfolio_risk_policy"] = "sentinel_portfolio_risk_policy"
    mechanism: SentinelPolicyApprovalMechanism
    verification_key_id: str | None = Field(default=None, min_length=1, max_length=160)
    signature_algorithm: SentinelPolicySignatureAlgorithm | None = None
    proof: str = Field(min_length=1, max_length=4096)
    approved_at: datetime
    effective_at: datetime
    expires_at: datetime
    scope: SentinelPolicyApprovalScope
    supersedes_approval_artifact_id: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("policy_id", "policy_version", "approver_id", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("verification_key_id", "proof", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("approved_at", "effective_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_claim(self) -> "SentinelPolicyApprovalArtifact":
        if not self.effective_at <= self.approved_at < self.expires_at:
            raise ValueError("approval time must fall within its validity interval")
        if self.mechanism is SentinelPolicyApprovalMechanism.DETACHED_SIGNATURE:
            if self.verification_key_id is None or self.signature_algorithm is None:
                raise ValueError("detached signature claims require a key and algorithm")
        elif self.verification_key_id is not None or self.signature_algorithm is not None:
            raise ValueError("non-signature approval claims cannot advertise signature metadata")
        return self

    @computed_field(return_type=str)
    @property
    def canonical_payload_fingerprint(self) -> str:
        return _fingerprint(
            self.model_dump(
                mode="json",
                exclude={"canonical_payload_fingerprint", "approval_artifact_id", "proof"},
            )
        )

    @computed_field(return_type=str)
    @property
    def approval_artifact_id(self) -> str:
        # Proof bytes are included in the artifact identity, while the payload
        # fingerprint remains the exact material an external signer/attestor
        # would need to approve.
        return _fingerprint(
            {
                "canonical_payload_fingerprint": self.canonical_payload_fingerprint,
                "proof": self.proof,
            }
        )


class SentinelPolicyApprovalState(SentinelPolicyApprovalModel):
    """Immutable lifecycle state for one approval artifact."""

    artifact: SentinelPolicyApprovalArtifact
    lifecycle: SentinelPolicyApprovalLifecycle = SentinelPolicyApprovalLifecycle.ACTIVE
    lifecycle_version: int = Field(default=1, ge=1)
    registered_at: datetime
    lifecycle_changed_at: datetime
    revoked_reason: str | None = Field(default=None, min_length=1, max_length=240)
    superseded_by_artifact_id: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("registered_at", "lifecycle_changed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "SentinelPolicyApprovalState":
        if self.lifecycle_changed_at < self.registered_at:
            raise ValueError("approval lifecycle change cannot precede registration")
        if self.lifecycle is SentinelPolicyApprovalLifecycle.ACTIVE:
            if self.lifecycle_version != 1 or self.revoked_reason is not None or self.superseded_by_artifact_id is not None:
                raise ValueError("active approval state cannot contain terminal metadata")
        elif self.lifecycle is SentinelPolicyApprovalLifecycle.REVOKED:
            if self.lifecycle_version < 2 or self.revoked_reason is None or self.superseded_by_artifact_id is not None:
                raise ValueError("revoked approval requires a later version and reason")
        elif self.lifecycle_version < 2 or self.superseded_by_artifact_id is None or self.revoked_reason is not None:
            raise ValueError("superseded approval requires a later version and replacement")
        if self.superseded_by_artifact_id == self.artifact.approval_artifact_id:
            raise ValueError("approval artifact cannot supersede itself")
        return self

    @computed_field(return_type=str)
    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"state_fingerprint"}))


class SentinelPolicyApprovalSourceResult(SentinelPolicyApprovalModel):
    status: SentinelPolicyApprovalSourceStatus
    source_id: str = Field(min_length=1, max_length=160)
    requested_policy_id: str = Field(min_length=1, max_length=160)
    requested_policy_version: str = Field(min_length=1, max_length=80)
    requested_artifact_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resolved_at: datetime
    state: SentinelPolicyApprovalState | None = None
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
    def validate_result(self) -> "SentinelPolicyApprovalSourceResult":
        if self.status is SentinelPolicyApprovalSourceStatus.AVAILABLE:
            if self.state is None or self.reason_codes:
                raise ValueError("available approval-source result requires one state and no failures")
            artifact = self.state.artifact
            if (
                artifact.policy_id != self.requested_policy_id
                or artifact.policy_version != self.requested_policy_version
                or artifact.approval_artifact_id != self.requested_artifact_id
            ):
                raise ValueError("approval-source result must bind the exact request")
        elif self.state is not None or not self.reason_codes:
            raise ValueError("unavailable approval-source result requires failures and no state")
        return self


class SentinelPolicyApprovalReadiness(SentinelPolicyApprovalModel):
    policy_source_configured: bool = False
    governance_contract_configured: bool = False
    approval_source_configured: bool = False
    revocation_status_available: bool = False
    trust_anchor_configured: bool = False
    approval_artifact_available: bool = False
    approver_verified: bool = False
    policy_fingerprint_verified: bool = False
    approval_lifecycle_valid: bool = False
    approval_scope_valid: bool = False
    production_policy_resolution_available: bool = False

    @model_validator(mode="after")
    def no_partial_production_readiness(self) -> "SentinelPolicyApprovalReadiness":
        required = (
            self.policy_source_configured,
            self.governance_contract_configured,
            self.approval_source_configured,
            self.revocation_status_available,
            self.trust_anchor_configured,
            self.approval_artifact_available,
            self.approver_verified,
            self.policy_fingerprint_verified,
            self.approval_lifecycle_valid,
            self.approval_scope_valid,
        )
        if self.production_policy_resolution_available and not all(required):
            raise ValueError("production policy resolution requires every readiness proof")
        return self


class SentinelPolicyApprovalVerification(SentinelPolicyApprovalModel):
    status: SentinelPolicyApprovalVerificationStatus
    artifact_id: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    artifact_fingerprint: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    approver_id: str | None = Field(default=None, min_length=1, max_length=160)
    verified_at: datetime
    readiness: SentinelPolicyApprovalReadiness
    reason_codes: tuple[str, ...] = ()
    execution_authority: Literal[False] = False

    @field_validator("verified_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_result(self) -> "SentinelPolicyApprovalVerification":
        successful = self.status in {
            SentinelPolicyApprovalVerificationStatus.AUTHORITATIVE,
            SentinelPolicyApprovalVerificationStatus.TEST_ONLY_VERIFIED,
        }
        if successful and (self.artifact_id is None or self.artifact_fingerprint is None or self.approver_id is None or self.reason_codes):
            raise ValueError("verified approval requires complete artifact lineage and no failures")
        if not successful and not self.reason_codes:
            raise ValueError("failed approval verification requires reason codes")
        if successful and not all(
            (
                self.readiness.policy_source_configured,
                self.readiness.approval_artifact_available,
                self.readiness.approver_verified,
                self.readiness.policy_fingerprint_verified,
                self.readiness.approval_lifecycle_valid,
                self.readiness.approval_scope_valid,
            )
        ):
            raise ValueError("verified approval requires complete source, artifact, and binding readiness")
        if self.status is SentinelPolicyApprovalVerificationStatus.AUTHORITATIVE:
            if not self.readiness.production_policy_resolution_available:
                raise ValueError("authoritative approval requires production readiness")
        elif self.readiness.production_policy_resolution_available:
            raise ValueError("non-authoritative approval cannot advertise production readiness")
        return self


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy approval timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
