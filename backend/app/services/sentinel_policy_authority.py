"""Fail-closed policy-source boundary for controlled-demo portfolio risk.

The repository currently has no independent policy approval authority.  The
default source is therefore unavailable.  A durable local catalog can preserve
policy history but remains non-authoritative, while test records require an
explicit test-only construction path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.sentinel_policy_authority import (
    SentinelPolicyAuthorityLevel,
    SentinelPolicyLifecycle,
    SentinelPolicyResolution,
    SentinelPolicyResolutionStatus,
    SentinelPortfolioRiskPolicyRecord,
)
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalReadiness,
    SentinelPolicyApprovalVerification,
    SentinelPolicyApprovalVerificationStatus,
)
from app.services.sentinel_policy_approval import (
    SentinelPolicyApprovalVerifier,
    UnavailableSentinelPolicyApprovalVerifier,
    approval_binding_codes,
)


CONTROLLED_DEMO_RISK_CALCULATION_VERSION = "controlled_demo_risk_v1"
_TEST_ONLY_CONSTRUCTION_TOKEN = object()


class SentinelPolicySourceError(RuntimeError):
    """A configured source cannot resolve a deterministic policy version."""


@runtime_checkable
class SentinelPortfolioRiskPolicySource(Protocol):
    @property
    def source_id(self) -> str: ...

    def resolve(
        self, *, policy_id: str, policy_version: str
    ) -> SentinelPortfolioRiskPolicyRecord | None: ...


class UnavailableSentinelPolicySource:
    source_id = "unconfigured_sentinel_portfolio_risk_policy_source"

    def resolve(
        self, *, policy_id: str, policy_version: str
    ) -> SentinelPortfolioRiskPolicyRecord | None:
        del policy_id, policy_version
        return None


class TestOnlySentinelPolicySource:
    """Deterministic fixture source; never accepted by production mode."""

    __test__ = False
    source_id = "test_only_sentinel_portfolio_risk_policy_source"

    def __init__(self, records: tuple[SentinelPortfolioRiskPolicyRecord, ...]) -> None:
        if not records:
            raise ValueError("test-only policy source requires records")
        by_key: dict[tuple[str, str], SentinelPortfolioRiskPolicyRecord] = {}
        for record in records:
            if record.authority_level is not SentinelPolicyAuthorityLevel.TEST_ONLY:
                raise ValueError("test-only policy source accepts only test-only records")
            key = (record.policy.policy_id, record.policy.policy_version)
            existing = by_key.get(key)
            if existing is not None and existing.record_fingerprint != record.record_fingerprint:
                raise ValueError("test-only policy source contains conflicting versions")
            by_key[key] = record
        self._records = dict(by_key)

    def resolve(
        self, *, policy_id: str, policy_version: str
    ) -> SentinelPortfolioRiskPolicyRecord | None:
        return self._records.get((policy_id, policy_version))


class SentinelPortfolioRiskPolicyVerifier:
    """Resolve exact policy content without inferring authority from a protocol.

    Arbitrary protocol implementations are rejected.  No production source is
    installed in this phase, so production-mode resolution is unavailable by
    default.  The explicit ``for_test`` factory is intentionally non-production
    and its result remains non-authorizing.
    """

    def __init__(
        self,
        source: SentinelPortfolioRiskPolicySource | None = None,
        *,
        approval_verifier: SentinelPolicyApprovalVerifier | None = None,
        _test_only_token: object | None = None,
    ) -> None:
        self._source = source or UnavailableSentinelPolicySource()
        self._approval_verifier = approval_verifier or UnavailableSentinelPolicyApprovalVerifier()
        self._allow_test_source = _test_only_token is _TEST_ONLY_CONSTRUCTION_TOKEN

    @classmethod
    def for_test(
        cls,
        records: tuple[SentinelPortfolioRiskPolicyRecord, ...],
        *,
        approval_verifier: SentinelPolicyApprovalVerifier,
    ) -> "SentinelPortfolioRiskPolicyVerifier":
        return cls(
            TestOnlySentinelPolicySource(records),
            approval_verifier=approval_verifier,
            _test_only_token=_TEST_ONLY_CONSTRUCTION_TOKEN,
        )

    @property
    def source_id(self) -> str:
        return self._source.source_id

    def resolve(
        self,
        *,
        candidate: ControlledDemoExecutionPolicy,
        evaluated_at: datetime,
    ) -> SentinelPolicyResolution:
        now = _utc(evaluated_at)
        # Protocol conformance is not a trust signal.  Until a reviewed
        # production source exists, accept only the built-in unavailable source
        # or the explicit test fixture type.
        if type(self._source) is UnavailableSentinelPolicySource:
            return self._failed(
                candidate, now, SentinelPolicyResolutionStatus.UNAVAILABLE,
                "SENTINEL_RISK_POLICY_AUTHORITY_UNAVAILABLE",
            )
        if type(self._source) is not TestOnlySentinelPolicySource or not self._allow_test_source:
            return self._failed(
                candidate, now, SentinelPolicyResolutionStatus.UNAVAILABLE,
                "SENTINEL_RISK_POLICY_SOURCE_UNTRUSTED",
            )
        try:
            record = self._source.resolve(
                policy_id=candidate.policy_id,
                policy_version=candidate.policy_version,
            )
        except Exception:
            return self._failed(
                candidate, now, SentinelPolicyResolutionStatus.UNAVAILABLE,
                "SENTINEL_RISK_POLICY_SOURCE_UNAVAILABLE",
            )
        if record is None:
            return self._failed(
                candidate, now, SentinelPolicyResolutionStatus.UNAVAILABLE,
                "SENTINEL_RISK_POLICY_VERSION_UNAVAILABLE",
            )
        codes = _record_codes(record=record, candidate=candidate, evaluated_at=now)
        approval_result = None
        if record.approval is not None:
            try:
                approval_result = self._approval_verifier.verify(
                    approval=record.approval,
                    policy=record.policy,
                    policy_source_configured=True,
                    evaluated_at=now,
                )
            except Exception:
                codes.append("SENTINEL_POLICY_APPROVAL_VERIFIER_UNAVAILABLE")
            else:
                if not isinstance(approval_result, SentinelPolicyApprovalVerification):
                    approval_result = None
                    codes.append("SENTINEL_POLICY_APPROVAL_VERIFIER_RESULT_INVALID")
                elif (
                    approval_result.artifact_id != record.approval.artifact.approval_artifact_id
                    or approval_result.artifact_fingerprint
                    != record.approval.artifact.canonical_payload_fingerprint
                    or approval_result.approver_id != record.approval.artifact.approver_id
                ):
                    codes.append("SENTINEL_POLICY_APPROVAL_VERIFICATION_LINEAGE_MISMATCH")
                elif approval_result.status is not SentinelPolicyApprovalVerificationStatus.TEST_ONLY_VERIFIED:
                    codes.extend(approval_result.reason_codes or ("SENTINEL_POLICY_APPROVAL_UNVERIFIED",))
                    if approval_result.status is SentinelPolicyApprovalVerificationStatus.AUTHORITATIVE:
                        codes.append("SENTINEL_POLICY_APPROVAL_TEST_BOUNDARY_VIOLATION")
        if codes:
            return SentinelPolicyResolution(
                status=SentinelPolicyResolutionStatus.REJECTED,
                authority_level=SentinelPolicyAuthorityLevel.TEST_ONLY,
                requested_policy_id=candidate.policy_id,
                requested_policy_version=candidate.policy_version,
                requested_policy_fingerprint=candidate.fingerprint,
                source_id=self.source_id,
                resolved_at=now,
                approval_readiness=(
                    approval_result.readiness
                    if approval_result is not None
                    else SentinelPolicyApprovalReadiness(
                        policy_source_configured=True,
                        approval_artifact_available=record.approval is not None,
                    )
                ),
                reason_codes=tuple(codes),
            )
        assert approval_result is not None
        return SentinelPolicyResolution(
            status=SentinelPolicyResolutionStatus.TEST_ONLY_VALIDATED,
            authority_level=SentinelPolicyAuthorityLevel.TEST_ONLY,
            requested_policy_id=candidate.policy_id,
            requested_policy_version=candidate.policy_version,
            requested_policy_fingerprint=candidate.fingerprint,
            source_id=self.source_id,
            resolved_at=now,
            record=record,
            approval_readiness=approval_result.readiness,
        )

    def _failed(
        self,
        candidate: ControlledDemoExecutionPolicy,
        now: datetime,
        status: SentinelPolicyResolutionStatus,
        code: str,
    ) -> SentinelPolicyResolution:
        return SentinelPolicyResolution(
            status=status,
            authority_level=SentinelPolicyAuthorityLevel.UNAVAILABLE,
            requested_policy_id=candidate.policy_id,
            requested_policy_version=candidate.policy_version,
            requested_policy_fingerprint=candidate.fingerprint,
            source_id=self.source_id,
            resolved_at=now,
            reason_codes=(code,),
        )


def _record_codes(
    *,
    record: SentinelPortfolioRiskPolicyRecord,
    candidate: ControlledDemoExecutionPolicy,
    evaluated_at: datetime,
) -> list[str]:
    codes: list[str] = []
    policy = record.policy
    if record.authority_level is not SentinelPolicyAuthorityLevel.TEST_ONLY:
        codes.append("SENTINEL_RISK_POLICY_AUTHORITY_UNAVAILABLE")
    if record.lifecycle is SentinelPolicyLifecycle.REVOKED:
        codes.append("SENTINEL_RISK_POLICY_REVOKED")
    elif record.lifecycle is SentinelPolicyLifecycle.SUPERSEDED:
        codes.append("SENTINEL_RISK_POLICY_SUPERSEDED")
    elif record.lifecycle is not SentinelPolicyLifecycle.ACTIVE:
        codes.append("SENTINEL_RISK_POLICY_LIFECYCLE_INVALID")
    if (
        policy.policy_id != candidate.policy_id
        or policy.policy_version != candidate.policy_version
        or policy.fingerprint != candidate.fingerprint
    ):
        codes.append("SENTINEL_RISK_POLICY_CONTENT_MISMATCH")
    if record.policy_content_fingerprint != policy.fingerprint:
        codes.append("SENTINEL_RISK_POLICY_FINGERPRINT_MISMATCH")
    if record.approval is None:
        codes.append("SENTINEL_RISK_POLICY_APPROVAL_BINDING_INVALID")
    else:
        codes.extend(
            approval_binding_codes(
                approval=record.approval,
                policy=policy,
                evaluated_at=evaluated_at,
            )
        )
    if record.calculation_version != CONTROLLED_DEMO_RISK_CALCULATION_VERSION:
        codes.append("SENTINEL_RISK_POLICY_CALCULATION_VERSION_MISMATCH")
    if policy.effective_at is None or policy.expires_at is None:
        codes.append("SENTINEL_RISK_POLICY_WINDOW_REQUIRED")
    elif not policy.effective_at <= evaluated_at < policy.expires_at:
        codes.append("SENTINEL_RISK_POLICY_NOT_EFFECTIVE")
    return sorted(set(codes))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy verification time requires timezone awareness")
    return value.astimezone(UTC)
