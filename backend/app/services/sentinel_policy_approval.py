"""Fail-closed independent approval-verification boundary for Sentinel policy.

The production verifier is deliberately unavailable: this repository contains
no authorized approver registry, public verification key, or external
governance client.  A protocol-shaped object or approval-shaped payload is not
a trust anchor.  Tests may inject a verifier only through the explicit
non-production policy-verifier factory, whose results remain non-authorizing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalLifecycle,
    SentinelPolicyApprovalReadiness,
    SentinelPolicyApprovalState,
    SentinelPolicyApprovalVerification,
    SentinelPolicyApprovalVerificationStatus,
)


@runtime_checkable
class SentinelPolicyApprovalVerifier(Protocol):
    def verify(
        self,
        *,
        approval: SentinelPolicyApprovalState,
        policy: ControlledDemoExecutionPolicy,
        policy_source_configured: bool,
        evaluated_at: datetime,
    ) -> SentinelPolicyApprovalVerification: ...


class UnavailableSentinelPolicyApprovalVerifier:
    """Production default until governance supplies a legitimate trust anchor."""

    def verify(
        self,
        *,
        approval: SentinelPolicyApprovalState,
        policy: ControlledDemoExecutionPolicy,
        policy_source_configured: bool,
        evaluated_at: datetime,
    ) -> SentinelPolicyApprovalVerification:
        del policy
        now = _utc(evaluated_at)
        return SentinelPolicyApprovalVerification(
            status=SentinelPolicyApprovalVerificationStatus.UNAVAILABLE,
            artifact_id=approval.artifact.approval_artifact_id,
            artifact_fingerprint=approval.artifact.canonical_payload_fingerprint,
            approver_id=approval.artifact.approver_id,
            verified_at=now,
            readiness=SentinelPolicyApprovalReadiness(
                policy_source_configured=policy_source_configured,
                approval_artifact_available=True,
            ),
            reason_codes=("SENTINEL_POLICY_APPROVAL_TRUST_ANCHOR_UNAVAILABLE",),
        )


def approval_binding_codes(
    *,
    approval: SentinelPolicyApprovalState,
    policy: ControlledDemoExecutionPolicy,
    evaluated_at: datetime,
) -> tuple[str, ...]:
    """Validate claim binding/lifecycle/scope, but never establish authority."""

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
    if approval.lifecycle is SentinelPolicyApprovalLifecycle.REVOKED:
        codes.append("SENTINEL_POLICY_APPROVAL_REVOKED")
    elif approval.lifecycle is SentinelPolicyApprovalLifecycle.SUPERSEDED:
        codes.append("SENTINEL_POLICY_APPROVAL_SUPERSEDED")
    elif approval.lifecycle is not SentinelPolicyApprovalLifecycle.ACTIVE:
        codes.append("SENTINEL_POLICY_APPROVAL_LIFECYCLE_INVALID")
    if not artifact.effective_at <= now < artifact.expires_at:
        codes.append("SENTINEL_POLICY_APPROVAL_NOT_EFFECTIVE")
    required_scope = {
        "account_id": policy.allowed_account.account_id.upper(),
        "execution_mode": policy.mode.value.upper(),
        "canonical_symbol": policy.instrument.canonical_symbol.upper(),
        "risk_currency": policy.risk_currency.upper(),
    }
    scope = artifact.scope
    if (
        required_scope["account_id"] not in scope.account_ids
        or required_scope["execution_mode"] not in scope.execution_modes
        or required_scope["canonical_symbol"] not in scope.canonical_symbols
        or required_scope["risk_currency"] not in scope.risk_currencies
    ):
        codes.append("SENTINEL_POLICY_APPROVAL_SCOPE_MISMATCH")
    return tuple(sorted(set(codes)))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy approval verification time requires timezone awareness")
    return value.astimezone(UTC)
