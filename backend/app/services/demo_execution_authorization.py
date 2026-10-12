"""Fail-closed pre-authorization for the first controlled demo execution.

An authorization is not an execution instruction.  This service deliberately
does not import Pilot, an execution adapter, or MetaTrader5.  It creates only
an immutable artifact that a separately reviewed future submission boundary
would have to verify again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.audit.models import AuditEvent, AuditEventType
from app.audit.service import AuditService
from app.audit.store import AuditStoreError
from app.execution.demo_authorization_store import DemoAuthorizationStore, DemoAuthorizationStoreError
from app.execution.kill_switch import ExecutionKillSwitch
from app.execution.state_store import ExecutionStateStore
from app.models.demo_authorization import (
    ControlledDemoAuthorization,
    DemoAuthorizationResult,
    DemoAuthorizationVerification,
)
from app.models.demo_execution import (
    ControlledDemoExecutionPolicy,
    DemoExecutionMode,
    DemoReadinessContext,
)
from app.models.execution import ExecutionIntent
from app.models.execution_state import ExecutionLifecycleState, build_execution_record
from app.models.proposals import SentinelDecision, TradeProposal
from app.services.demo_execution_readiness import ControlledDemoReadinessGate
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry, proposal_fingerprint


_PREAUTH_ONLY_CODE = "DEMO_CONTROLLED_DISABLED"
_SUBMISSION_OR_RECOVERY_STATES = frozenset(
    {
        ExecutionLifecycleState.SUBMISSION_IN_PROGRESS,
        ExecutionLifecycleState.SUBMITTED,
        ExecutionLifecycleState.ACKNOWLEDGED,
        ExecutionLifecycleState.PARTIALLY_FILLED,
        ExecutionLifecycleState.FILLED,
        ExecutionLifecycleState.CANCEL_REQUESTED,
        ExecutionLifecycleState.CANCELLED,
        ExecutionLifecycleState.UNKNOWN,
        ExecutionLifecycleState.RECONCILIATION_REQUIRED,
    }
)


class DemoExecutionAuthorizationService:
    """Create and verify a non-submittable, one-time demo authorization.

    The service uses existing Sentinel verification, controlled-demo readiness,
    kill-switch, execution-state, and audit components.  It deliberately has
    no submission dependency, so a normal application flow cannot cross into
    broker execution through this class.
    """

    def __init__(
        self,
        *,
        approval_registry: ApprovalRegistry,
        readiness_gate: ControlledDemoReadinessGate,
        kill_switch: ExecutionKillSwitch,
        authorization_store: DemoAuthorizationStore,
        execution_state_store: ExecutionStateStore | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._approval_registry = approval_registry
        self._readiness_gate = readiness_gate
        self._kill_switch = kill_switch
        self._authorization_store = authorization_store
        self._execution_state_store = execution_state_store
        self._audit_service = audit_service

    def authorize_demo_execution(
        self,
        *,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        context: DemoReadinessContext | None,
        requested_mode: DemoExecutionMode | str,
        runtime_execution_mode: str,
        execution_window_start: datetime,
        execution_window_end: datetime,
        now: datetime,
    ) -> DemoAuthorizationResult:
        """Create a pre-authorization artifact only after every safety check.

        ``submission_allowed`` is deliberately hard-coded false.  This method
        does not call Pilot, ``FinalExecutionGate``, adapters, or broker APIs.
        """
        now_utc = _require_aware(now)
        codes = self._authorization_codes(
            policy=policy,
            proposal=proposal,
            decision=decision,
            intent=intent,
            context=context,
            requested_mode=requested_mode,
            runtime_execution_mode=runtime_execution_mode,
            execution_window_start=execution_window_start,
            execution_window_end=execution_window_end,
            now=now_utc,
        )
        if codes:
            self._audit_outcome(
                name="authorization_rejected",
                now=now_utc,
                proposal=proposal,
                decision=decision,
                intent=intent,
                policy=policy,
                reason_code=codes[0],
            )
            return DemoAuthorizationResult(evaluated_at=now_utc, reason_codes=tuple(codes))

        assert proposal is not None and decision is not None and intent is not None and context is not None
        expiry = _earliest_expiry(
            proposal=proposal,
            decision=decision,
            intent=intent,
            policy=policy,
            context=context,
            execution_window_end=_require_aware(execution_window_end),
        )
        authorization = ControlledDemoAuthorization(
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_fingerprint=policy.fingerprint,
            account=policy.allowed_account,
            instrument=policy.instrument,
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal_fingerprint(proposal),
            analysis_id=proposal.analysis_id,
            evidence_fingerprint=proposal.evidence_fingerprint,
            approval_id=decision.approval_id or "",
            execution_intent_id=intent.execution_intent_id,
            execution_intent_fingerprint=intent.fingerprint,
            direction=intent.direction,
            quantity=intent.quantity or 0.0,
            order_type=intent.order_type or "",
            filling_mode=intent.filling_mode or "",
            created_at=now_utc,
            execution_window_start=_require_aware(execution_window_start),
            execution_window_end=_require_aware(execution_window_end),
            expires_at=expiry,
        )
        if self._prior_authorization_event_exists():
            self._audit_outcome(
                name="authorization_duplicate_blocked",
                now=now_utc,
                proposal=proposal,
                decision=decision,
                intent=intent,
                policy=policy,
                authorization=authorization,
                reason_code="DEMO_AUTHORIZATION_RECOVERY_REQUIRED",
            )
            return DemoAuthorizationResult(
                evaluated_at=now_utc,
                reason_codes=("DEMO_AUTHORIZATION_RECOVERY_REQUIRED",),
            )
        # The request event is recorded before a new registry entry exists. A
        # durable audit failure leaves no authorization artifact behind.
        if not self._audit_outcome(
            name="authorization_requested",
            now=now_utc,
            proposal=proposal,
            decision=decision,
            intent=intent,
            policy=policy,
            authorization=authorization,
            reason_code="DEMO_AUTHORIZATION_REQUESTED",
        ):
            return DemoAuthorizationResult(
                evaluated_at=now_utc,
                reason_codes=("AUDIT_PERSISTENCE_UNAVAILABLE",),
            )

        try:
            stored = self._authorization_store.create(authorization)
        except DemoAuthorizationStoreError as error:
            code = str(error)
            self._audit_outcome(
                name="authorization_duplicate_blocked" if "DUPLICATE" in code or "LIMIT" in code else "authorization_rejected",
                now=now_utc,
                proposal=proposal,
                decision=decision,
                intent=intent,
                policy=policy,
                reason_code=code,
            )
            return DemoAuthorizationResult(evaluated_at=now_utc, reason_codes=(code,))

        if not self._audit_outcome(
            name="authorization_created",
            now=now_utc,
            proposal=proposal,
            decision=decision,
            intent=intent,
            policy=policy,
            authorization=stored,
            reason_code="DEMO_AUTHORIZATION_CREATED",
        ):
            # Persisting a creation record is mandatory.  The immutable
            # registry item is deterministically revoked rather than left in a
            # usable state without a durable audit trail.
            self._authorization_store.revoke(
                authorization_id=stored.authorization_id,
                reason_code="AUDIT_PERSISTENCE_UNAVAILABLE",
                now=now_utc,
            )
            return DemoAuthorizationResult(
                evaluated_at=now_utc,
                reason_codes=("AUDIT_PERSISTENCE_UNAVAILABLE",),
            )
        return DemoAuthorizationResult(authorization=stored, evaluated_at=now_utc)

    def verify_authorization(
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
    ) -> DemoAuthorizationVerification:
        """Verify a future artifact without consuming it or permitting submit."""
        now_utc = _require_aware(now)
        try:
            authorization = self._authorization_store.verify(
                authorization_id=authorization_id,
                now=now_utc,
            )
        except DemoAuthorizationStoreError as error:
            code = str(error)
            self._audit_outcome(
                name="authorization_expired" if "EXPIRED" in code else "authorization_rejected",
                now=now_utc,
                proposal=proposal,
                decision=decision,
                intent=intent,
                policy=policy,
                reason_code=code,
            )
            return DemoAuthorizationVerification(verified_at=now_utc, reason_codes=(code,))

        codes = self._verification_codes(
            authorization=authorization,
            policy=policy,
            proposal=proposal,
            decision=decision,
            intent=intent,
            context=context,
            runtime_execution_mode=runtime_execution_mode,
            now=now_utc,
        )
        if codes:
            # A mismatched caller input, stale market, dead approval, changed
            # account binding, or reconciliation uncertainty invalidates this
            # artifact rather than permitting another attempt.
            try:
                revoked = self._authorization_store.revoke(
                    authorization_id=authorization.authorization_id,
                    reason_code=codes[0],
                    now=now_utc,
                )
                self._audit_outcome(
                    name="authorization_revoked",
                    now=now_utc,
                    proposal=proposal,
                    decision=decision,
                    intent=intent,
                    policy=policy,
                    authorization=revoked,
                    reason_code=codes[0],
                )
            except DemoAuthorizationStoreError:
                codes.append("DEMO_AUTHORIZATION_REVOCATION_FAILED")
            return DemoAuthorizationVerification(verified_at=now_utc, reason_codes=tuple(codes))

        self._audit_outcome(
            name="authorization_verified",
            now=now_utc,
            proposal=proposal,
            decision=decision,
            intent=intent,
            policy=policy,
            authorization=authorization,
            reason_code="DEMO_AUTHORIZATION_VERIFIED",
        )
        return DemoAuthorizationVerification(authorization=authorization, verified_at=now_utc)

    def revoke_authorization(
        self,
        *,
        authorization_id: str,
        reason_code: str,
        now: datetime,
    ) -> DemoAuthorizationVerification:
        """Explicit fail-closed revocation; it cannot create or consume authority."""
        now_utc = _require_aware(now)
        try:
            authorization = self._authorization_store.revoke(
                authorization_id=authorization_id,
                reason_code=reason_code,
                now=now_utc,
            )
        except DemoAuthorizationStoreError as error:
            return DemoAuthorizationVerification(verified_at=now_utc, reason_codes=(str(error),))
        self._audit_outcome(
            name="authorization_revoked",
            now=now_utc,
            proposal=None,
            decision=None,
            intent=None,
            policy=None,
            authorization=authorization,
            reason_code=reason_code,
        )
        return DemoAuthorizationVerification(authorization=authorization, verified_at=now_utc)

    def _authorization_codes(
        self,
        *,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        context: DemoReadinessContext | None,
        requested_mode: DemoExecutionMode | str,
        runtime_execution_mode: str,
        execution_window_start: datetime,
        execution_window_end: datetime,
        now: datetime,
    ) -> list[str]:
        codes: list[str] = []
        mode = requested_mode.value if isinstance(requested_mode, DemoExecutionMode) else str(requested_mode).strip().lower()
        if mode != DemoExecutionMode.DEMO_CONTROLLED.value:
            codes.append("DEMO_MODE_NOT_EXPLICITLY_REQUESTED")
        if str(runtime_execution_mode).strip().lower() != "disabled":
            codes.append("RUNTIME_EXECUTION_MODE_NOT_DISABLED")
        start = _maybe_aware(execution_window_start)
        end = _maybe_aware(execution_window_end)
        if start is None or end is None or start >= end:
            codes.append("DEMO_EXECUTION_WINDOW_INVALID")
        else:
            if not start <= now < end:
                codes.append("DEMO_EXECUTION_WINDOW_CLOSED")
            if end - start > timedelta(seconds=policy.controlled_window_seconds):
                codes.append("DEMO_EXECUTION_WINDOW_LIMIT")
        if policy.effective_at is None or policy.expires_at is None:
            codes.append("DEMO_POLICY_WINDOW_REQUIRED")
        elif not policy.effective_at <= now < policy.expires_at:
            codes.append("DEMO_POLICY_NOT_ACTIVE")
        if proposal is None:
            codes.append("DEMO_PROPOSAL_REQUIRED")
        elif proposal.expires_at is None or now >= proposal.expires_at:
            codes.append("DEMO_PROPOSAL_EXPIRED")
        if decision is None:
            codes.append("DEMO_SENTINEL_APPROVAL_REQUIRED")
        elif decision.expires_at is None or now >= decision.expires_at:
            codes.append("DEMO_SENTINEL_APPROVAL_EXPIRED")
        if intent is None:
            codes.append("DEMO_EXECUTION_INTENT_REQUIRED")
        elif now >= intent.expires_at:
            codes.append("DEMO_EXECUTION_INTENT_EXPIRED")
        if context is None:
            codes.append("DEMO_READINESS_CONTEXT_REQUIRED")

        if proposal is not None and decision is not None and intent is not None and context is not None:
            codes.extend(self._lineage_codes(policy=policy, proposal=proposal, decision=decision, intent=intent, context=context))
            readiness = self._readiness_gate.evaluate(
                policy=policy,
                proposal=proposal,
                decision=decision,
                intent=intent,
                context=context,
                now=now,
            )
            # A disabled runtime is expected for this pre-authorization stage;
            # every other readiness failure blocks artifact creation.
            codes.extend(code for code in readiness.reason_codes if code != _PREAUTH_ONLY_CODE)
            if self._kill_switch.status().active:
                codes.append("KILL_SWITCH_ACTIVE")
            codes.extend(self._state_codes(intent=intent, context=context, now=now))
            try:
                verified = self._approval_registry.verify(proposal=proposal, decision=decision, now=now)
                if not _approval_matches_intent(verified, intent):
                    codes.append("DEMO_SENTINEL_INTENT_LINEAGE_MISMATCH")
            except ApprovalClaimError:
                codes.append("DEMO_SENTINEL_APPROVAL_INVALID")
            if not codes:
                expiry = _earliest_expiry(
                    proposal=proposal,
                    decision=decision,
                    intent=intent,
                    policy=policy,
                    context=context,
                    execution_window_end=_require_aware(execution_window_end),
                )
                if expiry <= now:
                    codes.append("DEMO_AUTHORIZATION_EXPIRY_ELAPSED")
        return sorted(set(codes))

    def _verification_codes(
        self,
        *,
        authorization: ControlledDemoAuthorization,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        context: DemoReadinessContext | None,
        runtime_execution_mode: str,
        now: datetime,
    ) -> list[str]:
        codes: list[str] = []
        if str(runtime_execution_mode).strip().lower() != "disabled":
            codes.append("RUNTIME_EXECUTION_MODE_NOT_DISABLED")
        if now >= authorization.expires_at:
            codes.append("DEMO_AUTHORIZATION_EXPIRED")
        if proposal is None or decision is None or intent is None or context is None:
            codes.append("DEMO_AUTHORIZATION_VERIFICATION_INPUT_MISSING")
            return sorted(set(codes))
        if (
            authorization.policy_id != policy.policy_id
            or authorization.policy_version != policy.policy_version
            or authorization.policy_fingerprint != policy.fingerprint
            or authorization.account != policy.allowed_account
            or authorization.instrument != policy.instrument
        ):
            codes.append("DEMO_AUTHORIZATION_POLICY_OR_BINDING_MISMATCH")
        if (
            authorization.proposal_id != proposal.proposal_id
            or authorization.proposal_fingerprint != proposal_fingerprint(proposal)
            or authorization.analysis_id != proposal.analysis_id
            or authorization.evidence_fingerprint != proposal.evidence_fingerprint
            or authorization.approval_id != decision.approval_id
            or authorization.execution_intent_id != intent.execution_intent_id
            or authorization.execution_intent_fingerprint != intent.fingerprint
            or authorization.direction != intent.direction
            or authorization.quantity != intent.quantity
            or authorization.order_type != intent.order_type
            or authorization.filling_mode != intent.filling_mode
        ):
            codes.append("DEMO_AUTHORIZATION_EXACT_LINEAGE_MISMATCH")
        if (
            proposal.expires_at is not None
            and decision.expires_at is not None
            and policy.expires_at is not None
            and authorization.expires_at
            != _earliest_expiry(
                proposal=proposal,
                decision=decision,
                intent=intent,
                policy=policy,
                context=context,
                execution_window_end=authorization.execution_window_end,
            )
        ):
            codes.append("DEMO_AUTHORIZATION_EXPIRY_MISMATCH")
        codes.extend(
            self._authorization_codes(
                policy=policy,
                proposal=proposal,
                decision=decision,
                intent=intent,
                context=context,
                requested_mode=DemoExecutionMode.DEMO_CONTROLLED,
                runtime_execution_mode=runtime_execution_mode,
                execution_window_start=authorization.execution_window_start,
                execution_window_end=authorization.execution_window_end,
                now=now,
            )
        )
        return sorted(set(codes))

    @staticmethod
    def _lineage_codes(
        *,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal,
        decision: SentinelDecision,
        intent: ExecutionIntent,
        context: DemoReadinessContext,
    ) -> list[str]:
        codes: list[str] = []
        if context.account.account != policy.allowed_account or intent.account_id != policy.allowed_account.account_id:
            codes.append("DEMO_ACCOUNT_MISMATCH")
        if (
            intent.symbol != policy.instrument.canonical_symbol
            or context.instrument.canonical_symbol != policy.instrument.canonical_symbol
            or context.instrument.broker_symbol != policy.instrument.broker_symbol
        ):
            codes.append("DEMO_SYMBOL_MISMATCH")
        if (
            intent.proposal_id != proposal.proposal_id
            or intent.proposal_fingerprint != proposal_fingerprint(proposal)
            or intent.analysis_id != proposal.analysis_id
            or intent.evidence_fingerprint != proposal.evidence_fingerprint
            or intent.approval_id != decision.approval_id
            or intent.policy_id != decision.policy_id
            or intent.policy_fingerprint != decision.policy_fingerprint
            or intent.assessment_id != decision.assessment_id
            or intent.assessment_fingerprint != decision.assessment_fingerprint
        ):
            codes.append("DEMO_EXECUTION_INTENT_LINEAGE_MISMATCH")
        if decision.account_id != policy.allowed_account.account_id:
            codes.append("DEMO_SENTINEL_ACCOUNT_MISMATCH")
        if intent.quantity is None or intent.order_type is None or intent.filling_mode is None:
            codes.append("DEMO_EXECUTION_PARAMETERS_REQUIRED")
        return codes

    def _state_codes(self, *, intent: ExecutionIntent, context: DemoReadinessContext, now: datetime) -> list[str]:
        records = list(context.execution_history)
        if self._execution_state_store is not None:
            # An intentionally malformed intent must be rejected by the
            # readiness checks, not crash while deriving a lookup-only record
            # identity.  There is no state to trust for an invalid contract.
            try:
                expected = build_execution_record(intent, execution_mode=DemoExecutionMode.DEMO_CONTROLLED.value, now=now)
            except (TypeError, ValueError, ValidationError):
                expected = None
            if expected is not None:
                stored = self._execution_state_store.get(expected.execution_id)
                if stored is not None:
                    records.append(stored)
        matching = [record for record in records if record.execution_intent_id == intent.execution_intent_id]
        if any(record.state in _SUBMISSION_OR_RECOVERY_STATES for record in matching):
            return ["DEMO_EXECUTION_STATE_INVALID"]
        return []

    def _audit_outcome(
        self,
        *,
        name: str,
        now: datetime,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        policy: ControlledDemoExecutionPolicy | None,
        reason_code: str,
        authorization: ControlledDemoAuthorization | None = None,
    ) -> bool:
        if self._audit_service is None:
            # The authorization boundary requires a durable record when it is
            # asked to create authority; rejected diagnostic paths stay safe.
            return name != "authorization_requested" and name != "authorization_created"
        fingerprint = authorization.fingerprint if authorization is not None else (policy.fingerprint if policy is not None else None)
        try:
            self._audit_service.record(
                AuditEvent(
                    event_type=AuditEventType.DEMO_AUTHORIZATION,
                    occurred_at=now,
                    proposal_id=proposal.proposal_id if proposal is not None else None,
                    intent_id=intent.execution_intent_id if intent is not None else None,
                    approval_id=decision.approval_id if decision is not None else None,
                    account_reference=(authorization.account.account_reference if authorization is not None else None),
                    state=name,
                    reason_code=reason_code,
                    provenance_fingerprint=fingerprint,
                    details=(("submission_allowed", "false"), ("authorization_id", authorization.authorization_id if authorization is not None else "none")),
                )
            )
        except (AuditStoreError, TypeError, ValueError):
            return False
        return True

    def _prior_authorization_event_exists(self) -> bool:
        """Detect a prior durable authorization during process recovery.

        A fresh process-local store cannot prove that a preceding process did
        not create this identity.  The durable audit ledger is therefore the
        authoritative fail-closed recovery signal for the one permitted demo
        identity.  Any prior durable requested/created lifecycle means the
        process must be reconciled rather than creating another identity.
        """
        if self._audit_service is None:
            return False
        try:
            for event in self._audit_service.events():
                if event.event_type is not AuditEventType.DEMO_AUTHORIZATION:
                    continue
                if event.state in {
                    "authorization_requested",
                    "authorization_created",
                    "authorization_verified",
                    "authorization_revoked",
                    "authorization_expired",
                }:
                    return True
        except AuditStoreError:
            # The subsequent required request audit will fail closed.  Do not
            # treat an unavailable history as proof of a clean restart.
            return True
        return False


def _approval_matches_intent(verified: Any, intent: ExecutionIntent) -> bool:
    return (
        verified.approval_id == intent.approval_id
        and verified.proposal_id == intent.proposal_id
        and verified.proposal_fingerprint == intent.proposal_fingerprint
        and verified.analysis_id == intent.analysis_id
        and verified.evidence_fingerprint == intent.evidence_fingerprint
        and verified.policy_id == intent.policy_id
        and verified.policy_fingerprint == intent.policy_fingerprint
        and verified.assessment_id == intent.assessment_id
        and verified.assessment_fingerprint == intent.assessment_fingerprint
    )


def _earliest_expiry(
    *,
    proposal: TradeProposal,
    decision: SentinelDecision,
    intent: ExecutionIntent,
    policy: ControlledDemoExecutionPolicy,
    context: DemoReadinessContext,
    execution_window_end: datetime,
) -> datetime:
    assert proposal.expires_at is not None
    assert decision.expires_at is not None
    assert policy.expires_at is not None
    return min(
        proposal.expires_at,
        decision.expires_at,
        intent.expires_at,
        policy.expires_at,
        context.market_observed_at + timedelta(seconds=policy.max_market_age_seconds),
        execution_window_end,
    )


def _maybe_aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _require_aware(value: datetime) -> datetime:
    normalized = _maybe_aware(value)
    if normalized is None:
        raise ValueError("demo authorization timestamps require timezone awareness")
    return normalized
