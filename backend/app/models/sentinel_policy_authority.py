"""Immutable governance contracts for controlled-demo portfolio-risk policy.

A policy fingerprint proves deterministic content integrity only.  It is not
evidence that an operator or independent authority approved the policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.schemas import APIModel
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalMechanism,
    SentinelPolicyApprovalReadiness,
    SentinelPolicyApprovalState,
)


class SentinelPolicyAuthorityModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SentinelPolicyLifecycle(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class SentinelPolicyAuthorityLevel(str, Enum):
    UNAVAILABLE = "unavailable"
    LOCAL_GOVERNANCE_ONLY = "local_governance_only"
    TEST_ONLY = "test_only"
    INDEPENDENTLY_APPROVED = "independently_approved"


class SentinelPolicySourceKind(str, Enum):
    UNAVAILABLE = "unavailable"
    LOCAL_SQLITE_CATALOG = "local_sqlite_catalog"
    TEST_FIXTURE = "test_fixture"
    TRUSTED_GOVERNANCE = "trusted_governance"


class SentinelPolicyResolutionStatus(str, Enum):
    AUTHORITATIVE = "authoritative"
    TEST_ONLY_VALIDATED = "test_only_validated"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class SentinelPolicyProvenance(SentinelPolicyAuthorityModel):
    source_id: str = Field(min_length=1, max_length=160)
    source_kind: SentinelPolicySourceKind
    source_version: str = Field(min_length=1, max_length=80)
    metadata: tuple[tuple[str, str], ...] = ()

    @field_validator("source_id", "source_version", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("metadata")
    @classmethod
    def safe_metadata(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        forbidden = ("secret", "token", "password", "credential", "api_key", "apikey")
        result: list[tuple[str, str]] = []
        for key, item in value:
            normalized = key.strip().lower()
            if not normalized or any(word in normalized for word in forbidden):
                raise ValueError("policy provenance cannot contain sensitive fields")
            result.append((normalized, str(item).strip()))
        if len({key for key, _ in result}) != len(result):
            raise ValueError("policy provenance metadata keys must be unique")
        return tuple(sorted(result))


class SentinelPortfolioRiskPolicyRecord(SentinelPolicyAuthorityModel):
    """One versioned policy record plus honest provenance and lifecycle.

    ``LOCAL_GOVERNANCE_ONLY`` records are useful for deterministic replay, but
    are deliberately not production authority.  Only a future reviewed source
    may create an ``INDEPENDENTLY_APPROVED`` record.
    """

    schema_version: Literal["sentinel-portfolio-risk-policy/v1"] = (
        "sentinel-portfolio-risk-policy/v1"
    )
    policy: ControlledDemoExecutionPolicy
    calculation_id: Literal["controlled_demo_portfolio_risk"] = (
        "controlled_demo_portfolio_risk"
    )
    calculation_version: Literal["controlled_demo_risk_v1"] = "controlled_demo_risk_v1"
    lifecycle: SentinelPolicyLifecycle
    lifecycle_version: int = Field(ge=1)
    authority_level: SentinelPolicyAuthorityLevel
    provenance: SentinelPolicyProvenance
    approval: SentinelPolicyApprovalState | None = None
    registered_at: datetime
    lifecycle_changed_at: datetime
    revoked_reason: str | None = Field(default=None, min_length=1, max_length=160)
    supersedes_version: str | None = Field(default=None, min_length=1, max_length=80)
    superseded_by_version: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("registered_at", "lifecycle_changed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_governance(self) -> "SentinelPortfolioRiskPolicyRecord":
        if self.policy.effective_at is None or self.policy.expires_at is None:
            raise ValueError("governed policies require an explicit effective interval")
        if self.lifecycle_changed_at < self.registered_at:
            raise ValueError("policy lifecycle change cannot precede registration")
        if self.lifecycle is SentinelPolicyLifecycle.ACTIVE:
            if self.revoked_reason is not None or self.superseded_by_version is not None:
                raise ValueError("active policy cannot contain terminal lifecycle metadata")
        elif self.lifecycle is SentinelPolicyLifecycle.REVOKED:
            if self.revoked_reason is None or self.superseded_by_version is not None:
                raise ValueError("revoked policy requires a reason and cannot name a successor")
        elif self.superseded_by_version is None or self.revoked_reason is not None:
            raise ValueError("superseded policy requires a replacement version")
        if self.supersedes_version == self.policy.policy_version:
            raise ValueError("policy cannot supersede its own version")
        if self.superseded_by_version == self.policy.policy_version:
            raise ValueError("policy cannot be superseded by its own version")

        if self.authority_level is SentinelPolicyAuthorityLevel.LOCAL_GOVERNANCE_ONLY:
            if self.provenance.source_kind is not SentinelPolicySourceKind.LOCAL_SQLITE_CATALOG:
                raise ValueError("local policy records require local catalog provenance")
            if self.approval is not None:
                raise ValueError("local catalog records cannot claim independent approval")
        elif self.authority_level is SentinelPolicyAuthorityLevel.TEST_ONLY:
            if self.provenance.source_kind is not SentinelPolicySourceKind.TEST_FIXTURE:
                raise ValueError("test-only policy records require test-fixture provenance")
            if self.approval is None or self.approval.artifact.mechanism is not SentinelPolicyApprovalMechanism.TEST_FIXTURE:
                raise ValueError("test-only policy records require explicit test approval evidence")
        elif self.authority_level is SentinelPolicyAuthorityLevel.INDEPENDENTLY_APPROVED:
            if self.provenance.source_kind is not SentinelPolicySourceKind.TRUSTED_GOVERNANCE:
                raise ValueError("independently approved policies require trusted governance provenance")
            if self.approval is None or self.approval.artifact.mechanism is SentinelPolicyApprovalMechanism.TEST_FIXTURE:
                raise ValueError("independently approved policies require independent approval evidence")
        else:
            raise ValueError("unavailable authority cannot create a policy record")
        if self.approval is not None:
            artifact = self.approval.artifact
            if (
                artifact.policy_id != self.policy.policy_id
                or artifact.policy_version != self.policy.policy_version
                or artifact.policy_fingerprint != self.policy.fingerprint
            ):
                raise ValueError("policy approval evidence must bind the canonical policy identity and content")
        return self

    @computed_field(return_type=str)
    @property
    def policy_content_fingerprint(self) -> str:
        return self.policy.fingerprint

    @computed_field(return_type=str)
    @property
    def record_fingerprint(self) -> str:
        return _fingerprint(
            self.model_dump(
                mode="json",
                exclude={"policy_content_fingerprint", "record_fingerprint"},
            )
        )


class SentinelPolicyResolution(SentinelPolicyAuthorityModel):
    status: SentinelPolicyResolutionStatus
    authority_level: SentinelPolicyAuthorityLevel
    requested_policy_id: str = Field(min_length=1, max_length=160)
    requested_policy_version: str = Field(min_length=1, max_length=80)
    requested_policy_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_id: str = Field(min_length=1, max_length=160)
    resolved_at: datetime
    record: SentinelPortfolioRiskPolicyRecord | None = None
    approval_readiness: SentinelPolicyApprovalReadiness = SentinelPolicyApprovalReadiness()
    reason_codes: tuple[str, ...] = ()
    execution_authority: Literal[False] = False

    @field_validator("resolved_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_result(self) -> "SentinelPolicyResolution":
        successful = self.status in {
            SentinelPolicyResolutionStatus.AUTHORITATIVE,
            SentinelPolicyResolutionStatus.TEST_ONLY_VALIDATED,
        }
        if successful and (self.record is None or self.reason_codes):
            raise ValueError("successful policy resolution requires one record and no failures")
        if not successful and not self.reason_codes:
            raise ValueError("failed policy resolution requires reason codes")
        if self.status is SentinelPolicyResolutionStatus.AUTHORITATIVE:
            if self.authority_level is not SentinelPolicyAuthorityLevel.INDEPENDENTLY_APPROVED:
                raise ValueError("authoritative resolution requires independent policy approval")
            if not self.approval_readiness.production_policy_resolution_available:
                raise ValueError("authoritative resolution requires complete approval readiness")
        elif self.status is SentinelPolicyResolutionStatus.TEST_ONLY_VALIDATED:
            if self.authority_level is not SentinelPolicyAuthorityLevel.TEST_ONLY:
                raise ValueError("test-only resolution requires test-only authority level")
            if self.approval_readiness.production_policy_resolution_available:
                raise ValueError("test-only resolution cannot advertise production readiness")
        elif self.authority_level is SentinelPolicyAuthorityLevel.INDEPENDENTLY_APPROVED:
            raise ValueError("failed resolution cannot advertise independent authority")
        return self


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy governance timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
