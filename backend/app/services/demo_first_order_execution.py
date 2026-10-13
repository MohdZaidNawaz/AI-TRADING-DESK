"""Fail-closed synthetic first-demo boundary with no production execution state.

The coordinator is deliberately disconnected from Pilot, execution adapters,
and the production execution-state store. It can only hand a sanitized request
to a caller-supplied test seam after a test-only reservation exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Protocol

from app.audit.models import AuditEvent, AuditEventType
from app.audit.service import AuditService
from app.audit.store import AuditStoreError
from app.execution.demo_authorization_store import DemoAuthorizationStore, DemoAuthorizationStoreError
from app.models.demo_authorization import ControlledDemoAuthorization
from app.models.demo_execution import ControlledDemoExecutionPolicy, DemoReadinessContext
from app.models.demo_submission import (
    DemoSubmissionStatus,
    FirstDemoExecutionResult,
    FirstDemoOrderRequest,
    FirstDemoTestPermit,
    TestOnlyAdapterOutcome,
    TestOnlyAdapterResponse,
    TestOnlyBoundaryReservation,
    TestOnlyReservationStatus,
)
from app.models.execution import ExecutionIntent
from app.models.proposals import SentinelDecision, TradeDirection, TradeProposal
from app.services.demo_execution_authorization import DemoExecutionAuthorizationService
from app.services.execution_gate import FinalExecutionGate
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry, proposal_fingerprint


_FIRST_DEMO_VOLUME = Decimal("0.01")


class TestOnlyReservationStoreError(ValueError):
    """Synthetic reservation failure that can never authorize a broker call."""


class TestOnlyBoundaryReservationStore:
    """Bounded process-local reservation store for the fake test boundary.

    It is explicitly unsuitable for live execution: it has no broker adapter,
    no submission operation, and no durable restart guarantee. A restart must
    be treated as reconciliation uncertainty rather than a retry opportunity.
    """

    __test__ = False

    def __init__(self, *, max_records: int = 1) -> None:
        if max_records != 1:
            raise ValueError("test-only first-demo reservation store permits exactly one record")
        self._records: dict[str, TestOnlyBoundaryReservation] = {}
        self._recovery_uncertain = False
        self._lock = RLock()

    def reserve(
        self,
        *,
        permit: FirstDemoTestPermit,
        proposal: TradeProposal,
        intent: ExecutionIntent,
        now: datetime,
    ) -> tuple[TestOnlyBoundaryReservation, bool]:
        candidate = TestOnlyBoundaryReservation(
            permit_id=permit.permit_id,
            permit_fingerprint=permit.fingerprint,
            authorization_id=permit.authorization_id,
            execution_intent_id=intent.execution_intent_id,
            proposal_id=proposal.proposal_id,
            reserved_at=now,
        )
        with self._lock:
            if self._recovery_uncertain:
                raise TestOnlyReservationStoreError("TEST_BOUNDARY_RECOVERY_REQUIRED")
            existing = self._records.get(candidate.reservation_id)
            if existing is not None:
                if existing.fingerprint != candidate.fingerprint:
                    raise TestOnlyReservationStoreError("TEST_BOUNDARY_RESERVATION_CONFLICT")
                return existing, False
            if self._records:
                raise TestOnlyReservationStoreError("TEST_BOUNDARY_SINGLE_RESERVATION_LIMIT")
            self._records[candidate.reservation_id] = candidate
            return candidate, True

    def complete(
        self,
        *,
        reservation_id: str,
        status: TestOnlyReservationStatus,
    ) -> TestOnlyBoundaryReservation:
        if status is TestOnlyReservationStatus.RESERVED:
            raise ValueError("test-only reservation completion must be terminal")
        with self._lock:
            current = self._records.get(reservation_id)
            if current is None:
                raise TestOnlyReservationStoreError("TEST_BOUNDARY_RESERVATION_NOT_FOUND")
            updated = current.model_copy(update={"status": status})
            self._records[reservation_id] = updated
            return updated

    def mark_restart_uncertain(self) -> None:
        with self._lock:
            self._recovery_uncertain = True
            for key, reservation in tuple(self._records.items()):
                if reservation.status is TestOnlyReservationStatus.RESERVED:
                    self._records[key] = reservation.model_copy(
                        update={"status": TestOnlyReservationStatus.RECONCILIATION_REQUIRED}
                    )

    def get(self, reservation_id: str) -> TestOnlyBoundaryReservation | None:
        with self._lock:
            return self._records.get(reservation_id)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)


class TestOnlyFirstDemoAdapter(Protocol):
    """A deliberately non-broker seam, never an ExecutionAdapter."""

    test_only: bool

    def reach_test_boundary(self, request: FirstDemoOrderRequest) -> TestOnlyAdapterResponse: ...


class FirstDemoExecutionCoordinator:
    """Reach a fake test boundary once without production execution mutation."""

    def __init__(
        self,
        *,
        authorization_service: DemoExecutionAuthorizationService,
        authorization_store: DemoAuthorizationStore,
        approval_registry: ApprovalRegistry,
        final_execution_gate: FinalExecutionGate,
        reservation_store: TestOnlyBoundaryReservationStore,
        audit_service: AuditService | None,
        test_adapter: TestOnlyFirstDemoAdapter | None,
    ) -> None:
        self._authorization_service = authorization_service
        self._authorization_store = authorization_store
        self._approval_registry = approval_registry
        self._final_execution_gate = final_execution_gate
        self._reservation_store = reservation_store
        self._audit_service = audit_service
        self._test_adapter = test_adapter

    def attempt_test_only(
        self,
        *,
        permit: FirstDemoTestPermit,
        policy: ControlledDemoExecutionPolicy,
        proposal: TradeProposal | None,
        decision: SentinelDecision | None,
        intent: ExecutionIntent | None,
        context: DemoReadinessContext | None,
        runtime_execution_mode: str,
        now: datetime,
    ) -> FirstDemoExecutionResult:
        now_utc = _require_aware(now)
        codes = self._input_codes(
            permit=permit, policy=policy, proposal=proposal, decision=decision,
            intent=intent, context=context, runtime_execution_mode=runtime_execution_mode,
            now=now_utc,
        )
        verification = None
        if not codes:
            verification = self._authorization_service.verify_authorization(
                authorization_id=permit.authorization_id, policy=policy, proposal=proposal,
                decision=decision, intent=intent, context=context,
                runtime_execution_mode=runtime_execution_mode, now=now_utc,
            )
            codes.extend(verification.reason_codes)
        if verification is not None and verification.authorization is not None and proposal is not None and decision is not None and intent is not None:
            codes.extend(self._permit_codes(permit=permit, authorization=verification.authorization, proposal=proposal, intent=intent, now=now_utc))
            codes.extend(self._first_order_codes(policy=policy, intent=intent, context=context))
            # Read-only lineage, expiry, approval, and kill-switch verification.
            codes.extend(self._final_execution_gate.validate_non_submitting(proposal=proposal, decision=decision, intent=intent, now=now_utc))
        if self._test_adapter is None or getattr(self._test_adapter, "test_only", False) is not True or not callable(getattr(self._test_adapter, "reach_test_boundary", None)):
            codes.append("TEST_ONLY_ADAPTER_REQUIRED")
        if codes:
            self._audit(state="test_boundary_rejected", reason_code=sorted(set(codes))[0], now=now_utc, permit=permit, proposal=proposal, decision=decision, intent=intent)
            return self._result(permit=permit, status=DemoSubmissionStatus.REJECTED, now=now_utc, reason_codes=tuple(codes))

        assert proposal is not None and decision is not None and intent is not None
        try:
            reservation, created = self._reservation_store.reserve(permit=permit, proposal=proposal, intent=intent, now=now_utc)
        except TestOnlyReservationStoreError as error:
            return self._result(permit=permit, status=DemoSubmissionStatus.REJECTED, now=now_utc, reason_codes=(str(error),))
        if not created:
            return self._result(permit=permit, status=DemoSubmissionStatus.REJECTED, now=now_utc, reason_codes=("TEST_BOUNDARY_ALREADY_RESERVED",), reservation_id=reservation.reservation_id)

        # The durable audit receipt is required before irreversible test-only
        # consumption of Sentinel approval and demo authorization.
        if not self._audit(state="test_boundary_reserved", reason_code="TEST_BOUNDARY_RESERVED", now=now_utc, permit=permit, proposal=proposal, decision=decision, intent=intent):
            self._reservation_store.complete(reservation_id=reservation.reservation_id, status=TestOnlyReservationStatus.REJECTED)
            return self._result(permit=permit, status=DemoSubmissionStatus.REJECTED, now=now_utc, reason_codes=("AUDIT_PERSISTENCE_UNAVAILABLE",), reservation_id=reservation.reservation_id)
        try:
            self._approval_registry.claim(proposal=proposal, decision=decision, now=now_utc)
            self._authorization_store.consume_for_future_submission(authorization_id=permit.authorization_id, now=now_utc)
        except (ApprovalClaimError, DemoAuthorizationStoreError, ValueError):
            return self._reconciliation_required(
                permit=permit, proposal=proposal, decision=decision, intent=intent,
                reservation_id=reservation.reservation_id, now=now_utc,
                reason_code="ATTEMPT_RESERVATION_UNCERTAIN",
            )

        request = FirstDemoOrderRequest(
            direction=intent.direction, quantity=intent.quantity or 0.0,
            reference_price=intent.requested_entry, stop_loss=intent.stop_loss,
            take_profit=intent.take_profit, filling_mode=intent.filling_mode or "",
            execution_intent_id=intent.execution_intent_id, permit_id=permit.permit_id,
        )
        try:
            response = self._test_adapter.reach_test_boundary(request)
        except Exception:
            return self._reconciliation_required(
                permit=permit, proposal=proposal, decision=decision, intent=intent,
                reservation_id=reservation.reservation_id, now=now_utc,
                reason_code="TEST_BOUNDARY_CONNECTION_FAILURE",
            )
        return self._response_result(
            permit=permit, proposal=proposal, decision=decision, intent=intent,
            reservation_id=reservation.reservation_id, response=response, now=now_utc,
        )

    def recover_after_restart(self, *, permit: FirstDemoTestPermit, intent: ExecutionIntent, now: datetime) -> FirstDemoExecutionResult:
        """Record process-local uncertainty; this cannot recover or retry a boundary."""
        now_utc = _require_aware(now)
        self._reservation_store.mark_restart_uncertain()
        try:
            self._authorization_store.mark_reconciliation_required(authorization_id=permit.authorization_id, now=now_utc)
        except DemoAuthorizationStoreError:
            pass
        self._audit(state="test_boundary_restart_recovery", reason_code="RECONCILIATION_REQUIRED", now=now_utc, permit=permit, proposal=None, decision=None, intent=intent)
        return self._result(permit=permit, status=DemoSubmissionStatus.RECONCILIATION_REQUIRED, now=now_utc, reason_codes=("RECONCILIATION_REQUIRED",))

    def _response_result(self, *, permit: FirstDemoTestPermit, proposal: TradeProposal, decision: SentinelDecision, intent: ExecutionIntent, reservation_id: str, response: TestOnlyAdapterResponse, now: datetime) -> FirstDemoExecutionResult:
        if response.outcome is TestOnlyAdapterOutcome.REJECTED:
            self._reservation_store.complete(reservation_id=reservation_id, status=TestOnlyReservationStatus.REJECTED)
            self._audit(state="test_boundary_rejected", reason_code="TEST_BOUNDARY_REJECTED", now=now, permit=permit, proposal=proposal, decision=decision, intent=intent)
            return self._result(permit=permit, status=DemoSubmissionStatus.REJECTED, now=now, reason_codes=("TEST_BOUNDARY_REJECTED",), reservation_id=reservation_id, test_boundary_reached=True)
        reason = {
            TestOnlyAdapterOutcome.ACCEPTED: "TEST_BOUNDARY_REACHED",
            TestOnlyAdapterOutcome.TIMEOUT: "TEST_BOUNDARY_TIMEOUT",
            TestOnlyAdapterOutcome.CONNECTION_FAILURE: "TEST_BOUNDARY_CONNECTION_FAILURE",
            TestOnlyAdapterOutcome.UNKNOWN: "TEST_BOUNDARY_UNKNOWN",
        }[response.outcome]
        return self._reconciliation_required(
            permit=permit, proposal=proposal, decision=decision, intent=intent,
            reservation_id=reservation_id, now=now, reason_code=reason,
            test_boundary_reached=True,
        )

    def _reconciliation_required(self, *, permit: FirstDemoTestPermit, proposal: TradeProposal | None, decision: SentinelDecision | None, intent: ExecutionIntent, reservation_id: str | None, now: datetime, reason_code: str, test_boundary_reached: bool = False) -> FirstDemoExecutionResult:
        if reservation_id is not None:
            try:
                self._reservation_store.complete(reservation_id=reservation_id, status=TestOnlyReservationStatus.RECONCILIATION_REQUIRED)
            except TestOnlyReservationStoreError:
                pass
        try:
            self._authorization_store.mark_reconciliation_required(authorization_id=permit.authorization_id, now=now)
        except DemoAuthorizationStoreError:
            pass
        self._audit(state="test_boundary_reconciliation_required", reason_code=reason_code, now=now, permit=permit, proposal=proposal, decision=decision, intent=intent)
        return self._result(permit=permit, status=DemoSubmissionStatus.RECONCILIATION_REQUIRED, now=now, reason_codes=(reason_code, "RECONCILIATION_REQUIRED"), reservation_id=reservation_id, test_boundary_reached=test_boundary_reached)

    @staticmethod
    def _result(*, permit: FirstDemoTestPermit, status: DemoSubmissionStatus, now: datetime, reason_codes: tuple[str, ...], reservation_id: str | None = None, test_boundary_reached: bool = False) -> FirstDemoExecutionResult:
        return FirstDemoExecutionResult(permit_id=permit.permit_id, test_reservation_id=reservation_id, authorization_id=permit.authorization_id, status=status, evaluated_at=now, reason_codes=reason_codes, test_boundary_reached=test_boundary_reached)

    @staticmethod
    def _input_codes(*, permit: FirstDemoTestPermit, policy: ControlledDemoExecutionPolicy, proposal: TradeProposal | None, decision: SentinelDecision | None, intent: ExecutionIntent | None, context: DemoReadinessContext | None, runtime_execution_mode: str, now: datetime) -> list[str]:
        codes: list[str] = []
        if str(runtime_execution_mode).strip().lower() != "disabled":
            codes.append("RUNTIME_EXECUTION_MODE_NOT_DISABLED")
        if permit.test_only is not True or permit.scope != "first_demo_xauusd_test_only":
            codes.append("TEST_ONLY_PERMIT_REQUIRED")
        if not permit.issued_at <= now < permit.expires_at:
            codes.append("TEST_ONLY_PERMIT_EXPIRED")
        if proposal is None or decision is None or intent is None or context is None:
            codes.append("FIRST_DEMO_INPUTS_REQUIRED")
        if policy.enabled:
            codes.append("DEMO_CONTROLLED_MUST_REMAIN_DISABLED")
        return codes

    @staticmethod
    def _permit_codes(*, permit: FirstDemoTestPermit, authorization: ControlledDemoAuthorization, proposal: TradeProposal, intent: ExecutionIntent, now: datetime) -> list[str]:
        if now >= authorization.expires_at:
            return ["DEMO_AUTHORIZATION_EXPIRED"]
        if (permit.authorization_id != authorization.authorization_id or permit.authorization_fingerprint != authorization.fingerprint or permit.execution_intent_id != intent.execution_intent_id or permit.execution_intent_fingerprint != intent.fingerprint or permit.proposal_id != proposal.proposal_id or permit.proposal_fingerprint != proposal_fingerprint(proposal)):
            return ["TEST_ONLY_PERMIT_LINEAGE_MISMATCH"]
        return []

    @staticmethod
    def _first_order_codes(*, policy: ControlledDemoExecutionPolicy, intent: ExecutionIntent, context: DemoReadinessContext | None) -> list[str]:
        if context is None:
            return ["DEMO_READINESS_CONTEXT_REQUIRED"]
        codes: list[str] = []
        if _decimal(intent.quantity) != _FIRST_DEMO_VOLUME or _decimal(policy.max_order_volume) != _FIRST_DEMO_VOLUME:
            codes.append("FIRST_DEMO_VOLUME_MUST_BE_EXACTLY_0_01")
        instrument = context.instrument
        quantity = _decimal(intent.quantity)
        minimum, maximum, step = _decimal(instrument.minimum_volume), _decimal(instrument.maximum_volume), _decimal(instrument.volume_step)
        if quantity is None or minimum is None or maximum is None or step is None or step <= 0 or quantity < minimum or quantity > maximum or (quantity - minimum) % step != 0:
            codes.append("FIRST_DEMO_BROKER_VOLUME_INVALID")
        if intent.symbol != "XAUUSD" or instrument.canonical_symbol != "XAUUSD" or instrument.broker_symbol != "XAUUSD":
            codes.append("DEMO_SYMBOL_MISMATCH")
        if intent.order_type != "MARKET" or intent.order_type not in instrument.order_modes:
            codes.append("DEMO_UNSUPPORTED_ORDER_TYPE")
        if intent.filling_mode not in {"FOK", "IOC"} or intent.filling_mode not in instrument.filling_modes:
            codes.append("DEMO_UNSUPPORTED_FILLING_MODE")
        point = _decimal(instrument.point)
        values = (_decimal(intent.requested_entry), _decimal(intent.stop_loss), _decimal(intent.take_profit))
        if point is None or point <= 0 or any(value is None or value <= 0 or value % point != 0 for value in values):
            codes.append("DEMO_PRICE_PRECISION_INVALID")
        elif intent.direction is TradeDirection.LONG and not (values[1] < values[0] < values[2]):
            codes.append("DEMO_INVALID_PRICE_DIRECTION")
        elif intent.direction is TradeDirection.SHORT and not (values[2] < values[0] < values[1]):
            codes.append("DEMO_INVALID_PRICE_DIRECTION")
        return codes

    def _audit(self, *, state: str, reason_code: str, now: datetime, permit: FirstDemoTestPermit, proposal: TradeProposal | None, decision: SentinelDecision | None, intent: ExecutionIntent | None) -> bool:
        if self._audit_service is None:
            return False
        try:
            self._audit_service.record(AuditEvent(
                event_type=AuditEventType.DEMO_SUBMISSION_ATTEMPT,
                occurred_at=now, execution_id=permit.permit_id,
                proposal_id=proposal.proposal_id if proposal is not None else None,
                intent_id=intent.execution_intent_id if intent is not None else None,
                approval_id=decision.approval_id if decision is not None else None,
                account_reference=None, state=state, reason_code=reason_code,
                provenance_fingerprint=permit.fingerprint,
                details=(("test_only", "true"), ("broker_submission_attempted", "false")),
            ))
        except (AuditStoreError, TypeError, ValueError):
            return False
        return True


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("first demo execution timestamps require timezone awareness")
    return value.astimezone(UTC)
