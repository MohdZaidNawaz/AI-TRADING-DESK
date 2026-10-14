"""Formal fail-closed governance and approval-source boundary.

The interfaces describe the future authoritative sources.  The only production
implementations in this repository are explicitly unavailable.  Arbitrary
protocol implementations are not accepted as authority, and local persistence
or caller-supplied records cannot promote themselves into trusted governance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalLifecycle,
    SentinelPolicyApprovalReadiness,
    SentinelPolicyApprovalSourceResult,
    SentinelPolicyApprovalSourceStatus,
    SentinelPolicyApprovalState,
    SentinelPolicyApprovalVerification,
    SentinelPolicyApprovalVerificationStatus,
)
from app.models.sentinel_policy_governance import (
    SentinelPolicyGovernanceCapability,
    SentinelPolicyGovernanceLifecycle,
    SentinelPolicyGovernanceRecord,
    SentinelPolicyGovernanceSourceResult,
    SentinelPolicyGovernanceSourceStatus,
    SentinelPolicyGovernanceTrustLevel,
    SentinelPolicyTrustAnchorType,
)


@runtime_checkable
class SentinelPolicyGovernanceSource(Protocol):
    @property
    def source_id(self) -> str: ...

    def resolve(
        self, *, policy_id: str, policy_version: str, evaluated_at: datetime
    ) -> SentinelPolicyGovernanceSourceResult: ...


@runtime_checkable
class SentinelPolicyApprovalLifecycleSource(Protocol):
    @property
    def source_id(self) -> str: ...

    def resolve(
        self,
        *,
        policy_id: str,
        policy_version: str,
        artifact_id: str,
        evaluated_at: datetime,
    ) -> SentinelPolicyApprovalSourceResult: ...


class UnavailableSentinelPolicyGovernanceSource:
    source_id = "unconfigured_sentinel_policy_governance_source"

    def resolve(
        self, *, policy_id: str, policy_version: str, evaluated_at: datetime
    ) -> SentinelPolicyGovernanceSourceResult:
        return SentinelPolicyGovernanceSourceResult(
            status=SentinelPolicyGovernanceSourceStatus.UNAVAILABLE,
            source_id=self.source_id,
            requested_policy_id=policy_id,
            requested_policy_version=policy_version,
            resolved_at=_utc(evaluated_at),
            reason_codes=("SENTINEL_POLICY_GOVERNANCE_UNCONFIGURED",),
        )


class UnavailableSentinelPolicyApprovalLifecycleSource:
    source_id = "unconfigured_sentinel_policy_approval_lifecycle_source"

    def resolve(
        self,
        *,
        policy_id: str,
        policy_version: str,
        artifact_id: str,
        evaluated_at: datetime,
    ) -> SentinelPolicyApprovalSourceResult:
        return SentinelPolicyApprovalSourceResult(
            status=SentinelPolicyApprovalSourceStatus.UNAVAILABLE,
            source_id=self.source_id,
            requested_policy_id=policy_id,
            requested_policy_version=policy_version,
            requested_artifact_id=artifact_id,
            resolved_at=_utc(evaluated_at),
            reason_codes=("SENTINEL_POLICY_APPROVAL_SOURCE_UNAVAILABLE",),
        )


class SentinelPolicyGovernanceBoundary:
    """Resolve governance and revocation state without accepting injected trust."""

    def __init__(
        self,
        governance_source: SentinelPolicyGovernanceSource | None = None,
        approval_source: SentinelPolicyApprovalLifecycleSource | None = None,
    ) -> None:
        self._governance_source = governance_source or UnavailableSentinelPolicyGovernanceSource()
        self._approval_source = approval_source or UnavailableSentinelPolicyApprovalLifecycleSource()

    def assess(
        self,
        *,
        approval: SentinelPolicyApprovalState,
        policy: ControlledDemoExecutionPolicy,
        policy_source_configured: bool,
        evaluated_at: datetime,
    ) -> SentinelPolicyApprovalVerification:
        now = _utc(evaluated_at)
        artifact = approval.artifact
        codes: list[str] = []
        governance_result = None
        approval_result = None
        if type(self._governance_source) is not UnavailableSentinelPolicyGovernanceSource:
            codes.append("SENTINEL_POLICY_GOVERNANCE_SOURCE_UNTRUSTED")
        else:
            try:
                governance_result = self._governance_source.resolve(
                    policy_id=policy.policy_id,
                    policy_version=policy.policy_version,
                    evaluated_at=now,
                )
            except Exception:
                codes.append("SENTINEL_POLICY_GOVERNANCE_SOURCE_UNAVAILABLE")
            else:
                codes.extend(governance_result.reason_codes)
        if type(self._approval_source) is not UnavailableSentinelPolicyApprovalLifecycleSource:
            codes.append("SENTINEL_POLICY_APPROVAL_SOURCE_UNTRUSTED")
        else:
            try:
                approval_result = self._approval_source.resolve(
                    policy_id=policy.policy_id,
                    policy_version=policy.policy_version,
                    artifact_id=artifact.approval_artifact_id,
                    evaluated_at=now,
                )
            except Exception:
                codes.append("SENTINEL_POLICY_APPROVAL_SOURCE_UNAVAILABLE")
            else:
                codes.extend(approval_result.reason_codes)
        codes.append("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_UNAVAILABLE")
        return SentinelPolicyApprovalVerification(
            status=SentinelPolicyApprovalVerificationStatus.UNAVAILABLE,
            artifact_id=artifact.approval_artifact_id,
            artifact_fingerprint=artifact.canonical_payload_fingerprint,
            approver_id=artifact.approver_id,
            verified_at=now,
            readiness=SentinelPolicyApprovalReadiness(
                policy_source_configured=policy_source_configured,
                governance_contract_configured=(
                    governance_result is not None
                    and governance_result.status is SentinelPolicyGovernanceSourceStatus.AVAILABLE
                ),
                approval_source_configured=(
                    approval_result is not None
                    and approval_result.status is SentinelPolicyApprovalSourceStatus.AVAILABLE
                ),
                revocation_status_available=(
                    approval_result is not None
                    and approval_result.status is SentinelPolicyApprovalSourceStatus.AVAILABLE
                ),
                approval_artifact_available=True,
            ),
            reason_codes=tuple(codes),
        )


def governance_binding_codes(
    *,
    governance: SentinelPolicyGovernanceRecord,
    approval: SentinelPolicyApprovalState,
    policy: ControlledDemoExecutionPolicy,
    evaluated_at: datetime,
) -> tuple[str, ...]:
    """Validate exact governance bindings without asserting source authority."""

    now = _utc(evaluated_at)
    artifact = approval.artifact
    codes: list[str] = []
    if (
        artifact.policy_id != policy.policy_id
        or artifact.policy_version != policy.policy_version
    ):
        codes.append("SENTINEL_POLICY_APPROVAL_IDENTITY_MISMATCH")
    if artifact.policy_fingerprint != policy.fingerprint:
        codes.append("SENTINEL_POLICY_APPROVAL_FINGERPRINT_MISMATCH")
    if not artifact.effective_at <= now < artifact.expires_at:
        codes.append("SENTINEL_POLICY_APPROVAL_NOT_EFFECTIVE")
    approval_scope = artifact.scope
    if (
        policy.allowed_account.account_id.upper() not in approval_scope.account_ids
        or policy.mode.value.upper() not in approval_scope.execution_modes
        or policy.instrument.canonical_symbol not in approval_scope.canonical_symbols
        or policy.risk_currency not in approval_scope.risk_currencies
    ):
        codes.append("SENTINEL_POLICY_APPROVAL_SCOPE_MISMATCH")
    if governance.trust_level is not SentinelPolicyGovernanceTrustLevel.INDEPENDENTLY_VERIFIED:
        codes.append("SENTINEL_POLICY_GOVERNANCE_NOT_INDEPENDENTLY_VERIFIED")
    if governance.lifecycle is SentinelPolicyGovernanceLifecycle.REVOKED:
        codes.append("SENTINEL_POLICY_GOVERNANCE_REVOKED")
    elif governance.lifecycle is SentinelPolicyGovernanceLifecycle.SUPERSEDED:
        codes.append("SENTINEL_POLICY_GOVERNANCE_SUPERSEDED")
    elif governance.lifecycle is not SentinelPolicyGovernanceLifecycle.ACTIVE:
        codes.append("SENTINEL_POLICY_GOVERNANCE_INACTIVE")
    if governance.valid_from is None or governance.expires_at is None or not governance.valid_from <= now < governance.expires_at:
        codes.append("SENTINEL_POLICY_GOVERNANCE_NOT_EFFECTIVE")
    if governance.approver_id != artifact.approver_id:
        codes.append("SENTINEL_POLICY_GOVERNANCE_APPROVER_MISMATCH")
    if governance.required_approval_mechanism is not artifact.mechanism:
        codes.append("SENTINEL_POLICY_GOVERNANCE_MECHANISM_MISMATCH")
    required_capabilities = {
        SentinelPolicyGovernanceCapability.APPROVE_POLICY,
        SentinelPolicyGovernanceCapability.REVOKE_POLICY,
    }
    if not required_capabilities.issubset(set(governance.capabilities)):
        codes.append("SENTINEL_POLICY_GOVERNANCE_CAPABILITY_MISSING")
    scope = governance.scope
    if scope is None or (
        policy.policy_id not in scope.policy_ids
        or policy.policy_version not in scope.policy_versions
        or policy.mode.value.upper() not in scope.execution_modes
        or policy.allowed_account.account_id.upper() not in scope.account_ids
        or policy.instrument.canonical_symbol not in scope.canonical_symbols
    ):
        codes.append("SENTINEL_POLICY_GOVERNANCE_SCOPE_MISMATCH")
    anchor = governance.trust_anchor
    if anchor is None:
        codes.append("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_UNAVAILABLE")
    elif artifact.mechanism.value == "detached_signature":
        if (
            anchor.anchor_type is not SentinelPolicyTrustAnchorType.ED25519_PUBLIC_KEY
            or artifact.verification_key_id != anchor.anchor_id
            or artifact.signature_algorithm is not anchor.algorithm
        ):
            codes.append("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_MISMATCH")
    elif anchor.anchor_type is not SentinelPolicyTrustAnchorType.EXTERNAL_GOVERNANCE_SERVICE:
        codes.append("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_MISMATCH")
    if anchor is not None and anchor.expires_at is not None and now >= anchor.expires_at:
        codes.append("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_EXPIRED")
    if approval.lifecycle is not SentinelPolicyApprovalLifecycle.ACTIVE:
        codes.append("SENTINEL_POLICY_APPROVAL_LIFECYCLE_INVALID")
    return tuple(sorted(set(codes)))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy governance verification time requires timezone awareness")
    return value.astimezone(UTC)
