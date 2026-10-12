"""Non-submitting controlled-demo execution validation for Phase 12A.

The workflow is deliberately unable to invoke Pilot, an execution adapter, or
MetaTrader.  It verifies a prior immutable authorization and final execution
lineage, then records a dry-run validation result only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.audit.models import AuditEvent, AuditEventType
from app.audit.service import AuditService
from app.audit.store import AuditStoreError
from app.models.demo_dry_run import ControlledDemoDryRunResult, DemoDryRunStatus
from app.models.demo_execution import ControlledDemoExecutionPolicy, DemoReadinessContext
from app.models.execution import ExecutionIntent
from app.models.proposals import SentinelDecision, TradeProposal
from app.services.demo_execution_authorization import DemoExecutionAuthorizationService
from app.services.execution_gate import FinalExecutionGate
from app.services.trade_proposal_workflow import proposal_fingerprint


class ControlledDemoDryRunWorkflow:
    """Validate a pre-authorized demo intent without attempting submission.

    ``runtime_execution_mode`` must remain exactly ``disabled``.  A successful
    result means only that immutable synthetic inputs survived every required
    check; it never represents an adapter call, broker acknowledgement, or
    executable authority.
    """

    def __init__(
        self,
        *,
        authorization_service: DemoExecutionAuthorizationService,
        final_execution_gate: FinalExecutionGate,
        audit_service: AuditService | None,
    ) -> None:
        self._authorization_service = authorization_service
        self._final_execution_gate = final_execution_gate
        self._audit_service = audit_service

    def validate(
        self,
        *,
        authorization_id: str,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        context: DemoReadinessContext | None,
        runtime_execution_mode: str,
        now: datetime,
    ) -> ControlledDemoDryRunResult:
        now_utc = _require_aware(now)
        requested = self._audit(
            name="dry_run_requested",
            now=now_utc,
            authorization_id=authorization_id,
            proposal=proposal,
            decision=decision,
            intent=intent,
            policy=policy,
            reason_code="DEMO_DRY_RUN_REQUESTED",
        )
        if not requested:
            return self._result(
                status=DemoDryRunStatus.REJECTED,
                now=now_utc,
                authorization_id=authorization_id,
                proposal=proposal,
                intent=intent,
                reason_codes=("AUDIT_PERSISTENCE_UNAVAILABLE",),
            )

        codes: list[str] = []
        if str(runtime_execution_mode).strip().lower() != "disabled":
            codes.append("RUNTIME_EXECUTION_MODE_NOT_DISABLED")
        if decision is None or not decision.approval_id:
            codes.append("DEMO_SENTINEL_APPROVAL_REQUIRED")
        if intent is None:
            codes.append("DEMO_EXECUTION_INTENT_REQUIRED")
        else:
            if intent.direction is None:
                codes.append("DEMO_EXECUTION_DIRECTION_REQUIRED")
            if (
                intent.quantity is None
                or intent.order_type is None
                or intent.filling_mode is None
                or intent.requested_entry is None
                or intent.stop_loss is None
                or intent.take_profit is None
            ):
                codes.append("DEMO_EXECUTION_PARAMETERS_REQUIRED")

        verification = None
        # ``model_copy`` is intentionally used by regression tests to model
        # malformed external inputs.  Do not dereference a malformed intent's
        # computed identity while reporting its missing explicit direction.
        if intent is None or intent.direction is not None:
            verification = self._authorization_service.verify_authorization(
                authorization_id=authorization_id,
                policy=policy,
                proposal=proposal,
                decision=decision,
                intent=intent,
                context=context,
                runtime_execution_mode=runtime_execution_mode,
                now=now_utc,
            )
            codes.extend(verification.reason_codes)

        if verification is not None and verification.authorization is not None and proposal is not None and decision is not None and intent is not None:
            codes.extend(
                self._final_execution_gate.validate_non_submitting(
                    proposal=proposal,
                    decision=decision,
                    intent=intent,
                    now=now_utc,
                )
            )

        normalized_codes = tuple(sorted(set(codes)))
        status = DemoDryRunStatus.REJECTED if normalized_codes else DemoDryRunStatus.VALIDATED
        result = self._result(
            status=status,
            now=now_utc,
            authorization_id=authorization_id,
            authorization_fingerprint=(verification.authorization.fingerprint if verification is not None and verification.authorization is not None else None),
            proposal=proposal,
            intent=intent,
            reason_codes=normalized_codes,
        )
        event_name = "dry_run_validated" if status is DemoDryRunStatus.VALIDATED else "dry_run_rejected"
        if not self._audit(
            name=event_name,
            now=now_utc,
            authorization_id=authorization_id,
            proposal=proposal,
            decision=decision,
            intent=intent,
            policy=policy,
            reason_code=("DEMO_DRY_RUN_VALIDATED" if status is DemoDryRunStatus.VALIDATED else (normalized_codes[0] if normalized_codes else "DEMO_DRY_RUN_REJECTED")),
            authorization_fingerprint=result.authorization_fingerprint,
        ):
            return result.model_copy(
                update={
                    "status": DemoDryRunStatus.REJECTED,
                    "reason_codes": tuple(sorted(set((*result.reason_codes, "AUDIT_PERSISTENCE_UNAVAILABLE")))),
                }
            )
        return result

    @staticmethod
    def _result(
        *,
        status: DemoDryRunStatus,
        now: datetime,
        authorization_id: str | None,
        proposal: TradeProposal | None,
        intent: ExecutionIntent | None,
        reason_codes: tuple[str, ...],
        authorization_fingerprint: str | None = None,
    ) -> ControlledDemoDryRunResult:
        return ControlledDemoDryRunResult(
            authorization_id=authorization_id,
            authorization_fingerprint=authorization_fingerprint,
            proposal_id=proposal.proposal_id if proposal is not None else None,
            proposal_fingerprint=proposal_fingerprint(proposal) if proposal is not None else None,
            execution_intent_id=_safe_intent_value(intent, "execution_intent_id"),
            execution_intent_fingerprint=_safe_intent_value(intent, "fingerprint"),
            status=status,
            evaluated_at=now,
            reason_codes=reason_codes,
        )

    def _audit(
        self,
        *,
        name: str,
        now: datetime,
        authorization_id: str,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        policy: ControlledDemoExecutionPolicy,
        reason_code: str,
        authorization_fingerprint: str | None = None,
    ) -> bool:
        if self._audit_service is None:
            return False
        try:
            self._audit_service.record(
                AuditEvent(
                    event_type=AuditEventType.DEMO_DRY_RUN,
                    occurred_at=now,
                    execution_id=authorization_id,
                    proposal_id=proposal.proposal_id if proposal is not None else None,
                    intent_id=_safe_intent_value(intent, "execution_intent_id"),
                    approval_id=decision.approval_id if decision is not None else None,
                    account_reference=policy.allowed_account.account_reference,
                    state=name,
                    reason_code=reason_code,
                    provenance_fingerprint=authorization_fingerprint or policy.fingerprint,
                    details=(
                        ("authorization_id", authorization_id),
                        ("submission_attempted", "false"),
                        ("submission_allowed", "false"),
                        ("execution_state_mutated", "false"),
                    ),
                )
            )
        except (AuditStoreError, TypeError, ValueError):
            return False
        return True


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("demo dry-run timestamps require timezone awareness")
    return value.astimezone(UTC)


def _safe_intent_value(intent: ExecutionIntent | None, name: str) -> str | None:
    if intent is None:
        return None
    try:
        value = getattr(intent, name)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if isinstance(value, str) else None
