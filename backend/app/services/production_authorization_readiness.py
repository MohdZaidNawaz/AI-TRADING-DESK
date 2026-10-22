"""Central fail-closed production-authorization readiness diagnostics.

The gate performs read-only checks and has no success path in the current
repository.  It cannot initialize providers, mutate lifecycle state, consume
approval, submit an order, or convert model-shaped inputs into authority.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.audit.service import AuditService
from app.execution.kill_switch import ExecutionKillSwitch
from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.durable_execution import DurableExecutionReservation
from app.models.execution_state import ExecutionLifecycleState, ExecutionRecord
from app.models.production_authorization import (
    ProductionAuthorizationReadinessReport,
    ProductionReadinessCheck,
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.models.independent_trust_governance import (
    ExternalRollbackWitnessAssessment,
    ExternalRollbackWitnessStatus,
    IndependentTrustGovernanceAssessment,
    IndependentTrustGovernanceStatus,
)
from app.models.personal_trust_bootstrap import (
    PersonalKeyAuthorityAssessmentStatus,
    PersonalTrustBootstrapAssessment,
    PersonalTrustBootstrapStatus,
)
from app.models.proposals import SentinelDecision, TradeProposal
from app.models.sentinel_policy_authority import (
    SentinelPolicyLifecycle,
    SentinelPolicyResolution,
    SentinelPolicyResolutionStatus,
)
from app.models.sentinel_risk_evidence import (
    SentinelPortfolioRiskEvidence,
    SentinelRiskEvidenceStatus,
)
from app.models.trusted_execution_evidence import (
    ExecutionEvidenceAuthenticationStatus,
    ValidatedExecutionEvidence,
)
from app.services.durable_independent_replay import DurableIndependentReplayStore
from app.services.trade_proposal_workflow import ApprovalRegistry


class ProductionAuthorizationReadinessGate:
    """Aggregate independently useful diagnostics while always failing closed."""

    def __init__(
        self,
        approval_registry: ApprovalRegistry | None = None,
        *,
        audit_service: AuditService | None = None,
        kill_switch: ExecutionKillSwitch | None = None,
        replay_store: DurableIndependentReplayStore | None = None,
    ) -> None:
        # Only the concrete application facade owns durable trade approvals.
        # A protocol-shaped object is not accepted as a substitute.
        self._approval_registry = (
            approval_registry if type(approval_registry) is ApprovalRegistry else None
        )
        self._audit_service = audit_service if type(audit_service) is AuditService else None
        self._kill_switch = kill_switch if type(kill_switch) is ExecutionKillSwitch else None
        self._replay_store = (
            replay_store if type(replay_store) is DurableIndependentReplayStore else None
        )

    def evaluate(
        self,
        *,
        now: datetime,
        policy: ControlledDemoExecutionPolicy | None = None,
        policy_resolution: SentinelPolicyResolution | None = None,
        trusted_evidence: ValidatedExecutionEvidence | None = None,
        sentinel_risk_evidence: SentinelPortfolioRiskEvidence | None = None,
        proposal: TradeProposal | None = None,
        decision: SentinelDecision | None = None,
        reservation: DurableExecutionReservation | None = None,
        execution_records: tuple[ExecutionRecord, ...] = (),
        personal_trust: PersonalTrustBootstrapAssessment | None = None,
        trust_governance: IndependentTrustGovernanceAssessment | None = None,
        rollback_witness: ExternalRollbackWitnessAssessment | None = None,
    ) -> ProductionAuthorizationReadinessReport:
        checked_at = _utc(now)
        replay_checks = self._independent_replay(checked_at, rollback_witness)
        checks: list[ProductionReadinessCheck] = [
            _failed(
                ProductionReadinessPrerequisite.LOCAL_KEY_CUSTODY,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "LOCAL_KEY_CUSTODY_NOT_INDEPENDENTLY_ASSESSED",
                checked_at,
            ),
            _failed(
                ProductionReadinessPrerequisite.AUTHORIZED_POLICY_APPROVER,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "AUTHORIZED_POLICY_APPROVER_UNCONFIGURED",
                checked_at,
            ),
            self._independent_trust_anchor(trust_governance, checked_at),
            self._independent_anchor_lifecycle(trust_governance, checked_at),
            self._personal_trust_bootstrap(personal_trust, checked_at),
            self._personal_key_status_authority(personal_trust, checked_at),
            self._personal_replay_protection(personal_trust, checked_at),
            *replay_checks,
            _failed(
                ProductionReadinessPrerequisite.AUTHORITATIVE_APPROVAL_SOURCE,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "AUTHORITATIVE_POLICY_APPROVAL_SOURCE_UNAVAILABLE",
                checked_at,
            ),
            _failed(
                ProductionReadinessPrerequisite.AUTHORITATIVE_REVOCATION_SOURCE,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "AUTHORITATIVE_POLICY_REVOCATION_SOURCE_UNAVAILABLE",
                checked_at,
            ),
            self._policy_integrity(policy, policy_resolution, checked_at),
            self._trusted_evidence(trusted_evidence, checked_at),
            self._sentinel_risk(sentinel_risk_evidence, trusted_evidence, checked_at),
            self._sentinel_approval(proposal, decision, checked_at),
            self._reservation(reservation, checked_at),
            self._audit(checked_at),
            (
                _satisfied(
                    ProductionReadinessPrerequisite.KILL_SWITCH,
                    checked_at,
                    source_reference="process_local_kill_switch_inactive",
                )
                if self._kill_switch_inactive()
                else _failed(
                    ProductionReadinessPrerequisite.KILL_SWITCH,
                    ProductionReadinessCheckStatus.BLOCKED,
                    "KILL_SWITCH_ACTIVE_OR_UNAVAILABLE",
                    checked_at,
                )
            ),
            self._reconciliation(execution_records, checked_at),
            _failed(
                ProductionReadinessPrerequisite.SUBMISSION_BOUNDARY_REVIEW,
                ProductionReadinessCheckStatus.NOT_REVIEWED,
                "SUBMISSION_BOUNDARY_NOT_AUTHORIZED_FOR_PRODUCTION",
                checked_at,
            ),
            _failed(
                ProductionReadinessPrerequisite.EXPLICIT_EXECUTION_AUTHORIZATION,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "PRODUCTION_EXECUTION_AUTHORIZATION_UNAVAILABLE",
                checked_at,
            ),
            _failed(
                ProductionReadinessPrerequisite.EXECUTION_MODE,
                ProductionReadinessCheckStatus.BLOCKED,
                "LIVE_AND_DEMO_SUBMISSION_DISABLED",
                checked_at,
            ),
        ]
        ordered = tuple(sorted(checks, key=lambda item: item.prerequisite.value))
        reasons = tuple(
            sorted(
                {
                    code
                    for item in ordered
                    if item.status is not ProductionReadinessCheckStatus.SATISFIED
                    for code in item.reason_codes
                }
            )
        )
        return ProductionAuthorizationReadinessReport(
            evaluated_at=checked_at,
            checks=ordered,
            reason_codes=reasons,
        )

    def _independent_replay(
        self,
        now: datetime,
        witness: ExternalRollbackWitnessAssessment | None,
    ) -> tuple[ProductionReadinessCheck, ProductionReadinessCheck, ProductionReadinessCheck]:
        if self._replay_store is None:
            return (
                _failed(
                    ProductionReadinessPrerequisite.INDEPENDENT_REPLAY_STORAGE,
                    ProductionReadinessCheckStatus.UNAVAILABLE,
                    "DURABLE_INDEPENDENT_REPLAY_STORAGE_UNAVAILABLE",
                    now,
                ),
                _failed(
                    ProductionReadinessPrerequisite.ATOMIC_REPLAY_CONSUMPTION,
                    ProductionReadinessCheckStatus.UNAVAILABLE,
                    "ATOMIC_REPLAY_CONSUMPTION_UNAVAILABLE",
                    now,
                ),
                _failed(
                    ProductionReadinessPrerequisite.EXTERNAL_ROLLBACK_ASSURANCE,
                    ProductionReadinessCheckStatus.UNAVAILABLE,
                    "EXTERNAL_ROLLBACK_WITNESS_UNAVAILABLE",
                    now,
                ),
            )
        diagnostics = self._replay_store.diagnostics(checked_at=now)
        if diagnostics.durable_across_restart and diagnostics.atomic_consumption:
            local = (
                _satisfied(
                    ProductionReadinessPrerequisite.INDEPENDENT_REPLAY_STORAGE,
                    now,
                    source_reference="local_sqlite_replay_store",
                    evidence_fingerprint=diagnostics.fingerprint,
                ),
                _satisfied(
                    ProductionReadinessPrerequisite.ATOMIC_REPLAY_CONSUMPTION,
                    now,
                    source_reference="sqlite_begin_immediate_unique_claim",
                    evidence_fingerprint=diagnostics.fingerprint,
                ),
            )
        else:
            reasons = diagnostics.reason_codes or ("DURABLE_REPLAY_STORAGE_UNAVAILABLE",)
            local = (
                _failed_many(
                    ProductionReadinessPrerequisite.INDEPENDENT_REPLAY_STORAGE,
                    ProductionReadinessCheckStatus.UNAVAILABLE,
                    reasons,
                    now,
                    default="DURABLE_REPLAY_STORAGE_UNAVAILABLE",
                    evidence_fingerprint=diagnostics.fingerprint,
                ),
                _failed_many(
                    ProductionReadinessPrerequisite.ATOMIC_REPLAY_CONSUMPTION,
                    ProductionReadinessCheckStatus.UNAVAILABLE,
                    reasons,
                    now,
                    default="ATOMIC_REPLAY_CONSUMPTION_UNAVAILABLE",
                    evidence_fingerprint=diagnostics.fingerprint,
                ),
            )
        if witness is None:
            external = _failed(
                ProductionReadinessPrerequisite.EXTERNAL_ROLLBACK_ASSURANCE,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "EXTERNAL_ROLLBACK_WITNESS_UNAVAILABLE",
                now,
                evidence_fingerprint=diagnostics.fingerprint,
            )
        else:
            witness_status = {
                ExternalRollbackWitnessStatus.UNAVAILABLE: ProductionReadinessCheckStatus.UNAVAILABLE,
                ExternalRollbackWitnessStatus.STALE: ProductionReadinessCheckStatus.STALE,
                ExternalRollbackWitnessStatus.INVALID: ProductionReadinessCheckStatus.INVALID,
                ExternalRollbackWitnessStatus.INCONSISTENT: ProductionReadinessCheckStatus.INVALID,
                ExternalRollbackWitnessStatus.STRUCTURALLY_VALIDATED: ProductionReadinessCheckStatus.BLOCKED,
            }[witness.status]
            external = _failed_many(
                ProductionReadinessPrerequisite.EXTERNAL_ROLLBACK_ASSURANCE,
                witness_status,
                witness.reason_codes,
                now,
                default="EXTERNAL_ROLLBACK_WITNESS_UNAVAILABLE",
                source_reference=witness.provider_id,
                evidence_fingerprint=witness.fingerprint,
            )
        return (
            *local,
            external,
        )

    @staticmethod
    def _independent_trust_anchor(
        assessment: IndependentTrustGovernanceAssessment | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.INDEPENDENT_TRUST_ANCHOR
        if assessment is None:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "INDEPENDENT_TRUST_ANCHOR_UNAVAILABLE",
                now,
            )
        status = {
            IndependentTrustGovernanceStatus.UNCONFIGURED: ProductionReadinessCheckStatus.UNAVAILABLE,
            IndependentTrustGovernanceStatus.CONFIGURED_UNAUTHENTICATED: ProductionReadinessCheckStatus.BLOCKED,
            IndependentTrustGovernanceStatus.VALID_INDEPENDENTLY_AUTHENTICATED: ProductionReadinessCheckStatus.BLOCKED,
            IndependentTrustGovernanceStatus.EXPIRED: ProductionReadinessCheckStatus.STALE,
            IndependentTrustGovernanceStatus.REVOKED: ProductionReadinessCheckStatus.REVOKED,
            IndependentTrustGovernanceStatus.INVALID: ProductionReadinessCheckStatus.INVALID,
            IndependentTrustGovernanceStatus.ROTATION_PENDING: ProductionReadinessCheckStatus.BLOCKED,
            IndependentTrustGovernanceStatus.GOVERNANCE_UNAVAILABLE: ProductionReadinessCheckStatus.UNAVAILABLE,
        }[assessment.status]
        return _failed_many(
            prerequisite,
            status,
            assessment.reason_codes,
            now,
            default="INDEPENDENT_TRUST_ANCHOR_UNAVAILABLE",
            evidence_fingerprint=assessment.fingerprint,
        )

    @staticmethod
    def _independent_anchor_lifecycle(
        assessment: IndependentTrustGovernanceAssessment | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.INDEPENDENT_ANCHOR_LIFECYCLE
        if assessment is None:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "INDEPENDENT_ANCHOR_LIFECYCLE_UNAVAILABLE",
                now,
            )
        status = {
            IndependentTrustGovernanceStatus.UNCONFIGURED: ProductionReadinessCheckStatus.UNAVAILABLE,
            IndependentTrustGovernanceStatus.CONFIGURED_UNAUTHENTICATED: ProductionReadinessCheckStatus.BLOCKED,
            IndependentTrustGovernanceStatus.VALID_INDEPENDENTLY_AUTHENTICATED: ProductionReadinessCheckStatus.BLOCKED,
            IndependentTrustGovernanceStatus.EXPIRED: ProductionReadinessCheckStatus.STALE,
            IndependentTrustGovernanceStatus.REVOKED: ProductionReadinessCheckStatus.REVOKED,
            IndependentTrustGovernanceStatus.INVALID: ProductionReadinessCheckStatus.INVALID,
            IndependentTrustGovernanceStatus.ROTATION_PENDING: ProductionReadinessCheckStatus.BLOCKED,
            IndependentTrustGovernanceStatus.GOVERNANCE_UNAVAILABLE: ProductionReadinessCheckStatus.UNAVAILABLE,
        }[assessment.status]
        return _failed_many(
            prerequisite,
            status,
            assessment.reason_codes,
            now,
            default="INDEPENDENT_ANCHOR_LIFECYCLE_UNAVAILABLE",
            evidence_fingerprint=assessment.fingerprint,
        )

    @staticmethod
    def _personal_trust_bootstrap(
        assessment: PersonalTrustBootstrapAssessment | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.PERSONAL_TRUST_BOOTSTRAP
        if assessment is None:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "PERSONAL_TRUST_BOOTSTRAP_PENDING",
                now,
            )
        if assessment.status is PersonalTrustBootstrapStatus.LOCAL_CONFIRMATION_RECORDED:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.BLOCKED,
                "PERSONAL_TRUST_LOCAL_CONFIRMATION_NOT_INDEPENDENT",
                now,
                source_reference=assessment.ceremony_fingerprint,
                evidence_fingerprint=assessment.fingerprint,
            )
        return _failed_many(
            prerequisite,
            (
                ProductionReadinessCheckStatus.INVALID
                if assessment.status is PersonalTrustBootstrapStatus.INVALID
                else ProductionReadinessCheckStatus.BLOCKED
            ),
            assessment.reason_codes,
            now,
            default="PERSONAL_TRUST_BOOTSTRAP_PENDING",
            source_reference=assessment.ceremony_fingerprint,
            evidence_fingerprint=assessment.fingerprint,
        )

    @staticmethod
    def _personal_key_status_authority(
        assessment: PersonalTrustBootstrapAssessment | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.PERSONAL_KEY_STATUS_AUTHORITY
        if assessment is not None and assessment.authority_assessment_status in {
            PersonalKeyAuthorityAssessmentStatus.REVOKED,
            PersonalKeyAuthorityAssessmentStatus.COMPROMISED,
        }:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.REVOKED,
                "PERSONAL_KEY_REVOKED_OR_COMPROMISED",
                now,
                evidence_fingerprint=assessment.fingerprint,
            )
        if (
            assessment is not None
            and assessment.authority_assessment_status
            is PersonalKeyAuthorityAssessmentStatus.SUPERSEDED
        ):
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.SUPERSEDED,
                "PERSONAL_KEY_SUPERSEDED",
                now,
                evidence_fingerprint=assessment.fingerprint,
            )
        return _failed(
            prerequisite,
            ProductionReadinessCheckStatus.UNAVAILABLE,
            "AUTHORITATIVE_PERSONAL_KEY_STATUS_UNAVAILABLE",
            now,
            evidence_fingerprint=None if assessment is None else assessment.fingerprint,
        )

    @staticmethod
    def _personal_replay_protection(
        assessment: PersonalTrustBootstrapAssessment | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        return _failed(
            ProductionReadinessPrerequisite.PERSONAL_REPLAY_ROLLBACK_PROTECTION,
            ProductionReadinessCheckStatus.UNAVAILABLE,
            "DURABLE_PERSONAL_KEY_REPLAY_PROTECTION_UNAVAILABLE",
            now,
            evidence_fingerprint=None if assessment is None else assessment.fingerprint,
        )

    def _audit(self, now: datetime) -> ProductionReadinessCheck:
        try:
            available = self._audit_service is not None and self._audit_service.available()
        except Exception:
            available = False
        if available:
            return _satisfied(
                ProductionReadinessPrerequisite.AUDIT_DURABILITY,
                now,
                source_reference="durable_audit_store",
            )
        return _failed(
            ProductionReadinessPrerequisite.AUDIT_DURABILITY,
            ProductionReadinessCheckStatus.UNAVAILABLE,
            "AUDIT_DURABILITY_UNAVAILABLE",
            now,
        )

    def _kill_switch_inactive(self) -> bool:
        try:
            return self._kill_switch is not None and not self._kill_switch.status().active
        except Exception:
            return False

    @staticmethod
    def _policy_integrity(
        policy: ControlledDemoExecutionPolicy | None,
        resolution: SentinelPolicyResolution | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.POLICY_INTEGRITY
        if policy is None or resolution is None or resolution.record is None:
            codes = (
                ("POLICY_RESOLUTION_UNAVAILABLE",)
                if resolution is None
                else resolution.reason_codes or ("POLICY_RESOLUTION_UNAVAILABLE",)
            )
            status = _policy_failure_status(codes, resolution)
            return _failed_many(
                prerequisite,
                status,
                codes,
                now,
                source_reference=None if resolution is None else resolution.source_id,
            )
        record = resolution.record
        if (
            record.policy.policy_id != policy.policy_id
            or record.policy.policy_version != policy.policy_version
            or record.policy.fingerprint != policy.fingerprint
            or record.policy_content_fingerprint != policy.fingerprint
        ):
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.INVALID,
                "POLICY_IDENTITY_OR_FINGERPRINT_MISMATCH",
                now,
                source_reference=resolution.source_id,
            )
        if record.lifecycle is SentinelPolicyLifecycle.REVOKED:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.REVOKED,
                "POLICY_REVOKED",
                now,
                source_reference=resolution.source_id,
            )
        if record.lifecycle is SentinelPolicyLifecycle.SUPERSEDED:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.SUPERSEDED,
                "POLICY_SUPERSEDED",
                now,
                source_reference=resolution.source_id,
            )
        if (
            record.policy.effective_at is None
            or record.policy.expires_at is None
            or not record.policy.effective_at <= now < record.policy.expires_at
        ):
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.STALE,
                "POLICY_NOT_EFFECTIVE",
                now,
                source_reference=resolution.source_id,
            )
        return _satisfied(
            prerequisite,
            now,
            source_reference=resolution.source_id,
            evidence_fingerprint=policy.fingerprint,
        )

    @staticmethod
    def _trusted_evidence(
        evidence: ValidatedExecutionEvidence | None, now: datetime
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.TRUSTED_EXECUTION_EVIDENCE
        if evidence is None or not evidence.ready or evidence.source_observed_at is None:
            return _failed_many(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                () if evidence is None else evidence.reason_codes,
                now,
                default="TRUSTED_EXECUTION_EVIDENCE_UNAVAILABLE",
                source_reference=None if evidence is None else evidence.provider_id,
            )
        age = (now - evidence.source_observed_at).total_seconds()
        if age < 0 or age > evidence.request.max_age_seconds:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.STALE,
                "TRUSTED_EXECUTION_EVIDENCE_STALE",
                now,
                source_reference=evidence.provider_id,
                evidence_fingerprint=evidence.fingerprint,
            )
        if evidence.provider_authentication_status is not ExecutionEvidenceAuthenticationStatus.UNAUTHENTICATED:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "TRUSTED_EXECUTION_EVIDENCE_AUTHENTICATION_UNAVAILABLE",
                now,
                source_reference=evidence.provider_id,
                evidence_fingerprint=evidence.fingerprint,
            )
        # Provider identity and structural completeness do not authenticate the
        # source.  No independently authenticated broker-evidence provider is
        # configured in this repository, so fresh structural evidence must not
        # satisfy the production prerequisite.
        return _failed(
            prerequisite,
            ProductionReadinessCheckStatus.UNAVAILABLE,
            "TRUSTED_EXECUTION_EVIDENCE_PROVIDER_UNAUTHENTICATED",
            now,
            source_reference=evidence.provider_id,
            evidence_fingerprint=evidence.fingerprint,
        )

    @staticmethod
    def _sentinel_risk(
        risk: SentinelPortfolioRiskEvidence | None,
        evidence: ValidatedExecutionEvidence | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.SENTINEL_RISK_VERIFICATION
        if risk is None or risk.status is not SentinelRiskEvidenceStatus.VALIDATED:
            return _failed_many(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                () if risk is None else risk.reason_codes,
                now,
                default="SENTINEL_RISK_VERIFICATION_UNAVAILABLE",
            )
        if evidence is None or risk.trusted_evidence_fingerprint != evidence.fingerprint:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.INVALID,
                "SENTINEL_RISK_EVIDENCE_LINEAGE_MISMATCH",
                now,
                evidence_fingerprint=risk.fingerprint,
            )
        if risk.policy_authority_level.value != "independently_approved":
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.BLOCKED,
                "SENTINEL_RISK_POLICY_NOT_PRODUCTION_AUTHORITATIVE",
                now,
                source_reference=risk.policy_source_id,
                evidence_fingerprint=risk.fingerprint,
            )
        return _satisfied(
            prerequisite,
            now,
            source_reference=risk.policy_source_id,
            evidence_fingerprint=risk.fingerprint,
        )

    def _sentinel_approval(
        self,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        now: datetime,
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.SENTINEL_TRADE_APPROVAL
        if self._approval_registry is None or proposal is None or decision is None:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "DURABLE_SENTINEL_APPROVAL_UNAVAILABLE",
                now,
            )
        try:
            verified = self._approval_registry.verify(
                proposal=proposal,
                decision=decision,
                now=now,
            )
        except Exception:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.INVALID,
                "DURABLE_SENTINEL_APPROVAL_INVALID",
                now,
            )
        return _satisfied(
            prerequisite,
            now,
            source_reference=verified.approval_id,
            evidence_fingerprint=verified.evidence_fingerprint,
        )

    @staticmethod
    def _reservation(
        reservation: DurableExecutionReservation | None, now: datetime
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.DURABLE_RESERVATION
        if reservation is None:
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.UNAVAILABLE,
                "DURABLE_RESERVATION_UNAVAILABLE",
                now,
            )
        # The only current contract is structurally test-only. It is useful
        # evidence of duplicate protection but never a production permit.
        return _failed(
            prerequisite,
            ProductionReadinessCheckStatus.BLOCKED,
            "DURABLE_RESERVATION_TEST_ONLY",
            now,
            source_reference=reservation.reservation_id,
            evidence_fingerprint=reservation.fingerprint,
        )

    @staticmethod
    def _reconciliation(
        records: tuple[ExecutionRecord, ...], now: datetime
    ) -> ProductionReadinessCheck:
        prerequisite = ProductionReadinessPrerequisite.BROKER_RECONCILIATION
        if any(
            record.state is ExecutionLifecycleState.RECONCILIATION_REQUIRED
            for record in records
        ):
            return _failed(
                prerequisite,
                ProductionReadinessCheckStatus.BLOCKED,
                "UNRESOLVED_RECONCILIATION",
                now,
            )
        return _failed(
            prerequisite,
            ProductionReadinessCheckStatus.UNAVAILABLE,
            "AUTHORITATIVE_BROKER_RECONCILIATION_UNAVAILABLE",
            now,
        )


def _satisfied(
    prerequisite: ProductionReadinessPrerequisite,
    now: datetime,
    *,
    source_reference: str | None = None,
    evidence_fingerprint: str | None = None,
) -> ProductionReadinessCheck:
    return ProductionReadinessCheck(
        prerequisite=prerequisite,
        status=ProductionReadinessCheckStatus.SATISFIED,
        source_reference=source_reference,
        evidence_fingerprint=evidence_fingerprint,
        checked_at=now,
    )


def _failed(
    prerequisite: ProductionReadinessPrerequisite,
    status: ProductionReadinessCheckStatus,
    code: str,
    now: datetime,
    *,
    source_reference: str | None = None,
    evidence_fingerprint: str | None = None,
) -> ProductionReadinessCheck:
    return _failed_many(
        prerequisite,
        status,
        (code,),
        now,
        source_reference=source_reference,
        evidence_fingerprint=evidence_fingerprint,
    )


def _failed_many(
    prerequisite: ProductionReadinessPrerequisite,
    status: ProductionReadinessCheckStatus,
    codes: tuple[str, ...],
    now: datetime,
    *,
    default: str | None = None,
    source_reference: str | None = None,
    evidence_fingerprint: str | None = None,
) -> ProductionReadinessCheck:
    reasons = tuple(codes) or (() if default is None else (default,))
    return ProductionReadinessCheck(
        prerequisite=prerequisite,
        status=status,
        reason_codes=reasons,
        source_reference=source_reference,
        evidence_fingerprint=evidence_fingerprint,
        checked_at=now,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production readiness time requires timezone awareness")
    return value.astimezone(UTC)


def _policy_failure_status(
    codes: tuple[str, ...],
    resolution: SentinelPolicyResolution | None,
) -> ProductionReadinessCheckStatus:
    code_set = set(codes)
    if code_set & {
        "SENTINEL_RISK_POLICY_REVOKED",
        "SENTINEL_POLICY_APPROVAL_REVOKED",
        "POLICY_REVOKED",
    }:
        return ProductionReadinessCheckStatus.REVOKED
    if code_set & {
        "SENTINEL_RISK_POLICY_SUPERSEDED",
        "SENTINEL_POLICY_APPROVAL_SUPERSEDED",
        "POLICY_SUPERSEDED",
    }:
        return ProductionReadinessCheckStatus.SUPERSEDED
    if code_set & {
        "SENTINEL_RISK_POLICY_NOT_EFFECTIVE",
        "SENTINEL_POLICY_APPROVAL_NOT_EFFECTIVE",
        "POLICY_NOT_EFFECTIVE",
        "POLICY_EXPIRED",
    }:
        return ProductionReadinessCheckStatus.STALE
    if resolution is not None and resolution.status is SentinelPolicyResolutionStatus.REJECTED:
        return ProductionReadinessCheckStatus.INVALID
    return ProductionReadinessCheckStatus.UNAVAILABLE
