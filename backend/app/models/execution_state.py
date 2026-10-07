"""Immutable provider-neutral Phase 10 execution-control contracts.

Records describe an approved intent's safety lifecycle.  They neither submit
orders nor contain broker payloads, credentials, or trading decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator

from app.models.execution import ExecutionIntent, ExecutionResult
from app.models.schemas import APIModel


class ExecutionStateModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionLifecycleState(str, Enum):
    INTENT_CREATED = "intent_created"
    VALIDATED = "validated"
    READY_FOR_SUBMISSION = "ready_for_submission"
    DRY_RUN_VALIDATED = "dry_run_validated"
    SUBMISSION_IN_PROGRESS = "submission_in_progress"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    BLOCKED = "blocked"
    KILL_SWITCHED = "kill_switched"


class ReconciliationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    MATCHED = "matched"
    MISSING_BROKER_ORDER = "missing_broker_order"
    UNKNOWN_BROKER_ORDER = "unknown_broker_order"
    PARAMETER_MISMATCH = "parameter_mismatch"
    PARTIAL_FILL = "partial_fill"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class BrokerOrderObservation(ExecutionStateModel):
    """Sanitized provider-neutral broker state used only for reconciliation."""

    broker_order_id: str | None = Field(default=None, max_length=160)
    broker_position_id: str | None = Field(default=None, max_length=160)
    canonical_symbol: str | None = Field(default=None, max_length=40)
    broker_symbol: str | None = Field(default=None, max_length=80)
    direction: str | None = Field(default=None, max_length=16)
    requested_quantity: FiniteFloat | None = None
    filled_quantity: FiniteFloat | None = None
    remaining_quantity: FiniteFloat | None = None
    requested_price: FiniteFloat | None = None
    actual_price: FiniteFloat | None = None
    stop_loss: FiniteFloat | None = None
    take_profit: FiniteFloat | None = None
    status: str = Field(min_length=1, max_length=80)
    account_id: str | None = Field(default=None, max_length=160)


class ExecutionRecord(ExecutionStateModel):
    """Append-only-style immutable snapshot of one controlled execution attempt."""

    execution_id: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=64, max_length=64)
    attempt_identity: str = Field(min_length=64, max_length=64)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    analysis_id: str = Field(min_length=1, max_length=120)
    approval_id: str = Field(min_length=1, max_length=160)
    policy_id: str = Field(min_length=1, max_length=160)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    assessment_id: str = Field(min_length=1, max_length=160)
    assessment_fingerprint: str = Field(min_length=64, max_length=64)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    account_id: str = Field(min_length=1, max_length=160)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str | None = Field(default=None, max_length=80)
    direction: str = Field(min_length=1, max_length=16)
    quantity: FiniteFloat | None = None
    requested_price: FiniteFloat
    stop_loss: FiniteFloat
    take_profit: FiniteFloat
    execution_mode: str = Field(min_length=1, max_length=32)
    state: ExecutionLifecycleState
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    broker_order_id: str | None = Field(default=None, max_length=160)
    broker_position_id: str | None = Field(default=None, max_length=160)
    last_broker_status: str | None = Field(default=None, max_length=80)
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.NOT_REQUIRED
    failure_reason: str | None = Field(default=None, max_length=120)
    lifecycle_version: int = Field(ge=1)

    @field_validator("created_at", "updated_at", "submitted_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution state timestamps require timezone")
        return value.astimezone(UTC)

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            self.model_dump(mode="json", exclude={"created_at", "updated_at", "fingerprint"})
        )


class ExecutionTransition(ExecutionStateModel):
    execution_id: str
    from_state: ExecutionLifecycleState
    to_state: ExecutionLifecycleState
    reason_code: str | None = Field(default=None, max_length=120)
    occurred_at: datetime
    lifecycle_version: int = Field(ge=1)

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("transition timestamps require timezone")
        return value.astimezone(UTC)


class ExecutionSafetyDecision(ExecutionStateModel):
    """Final non-authorizing gate result. ``submission_allowed`` is Phase 10 false."""

    execution: ExecutionRecord
    submission_allowed: bool = False
    reason_codes: tuple[str, ...] = ()
    adapter_result: ExecutionResult | None = None


class ReconciliationReport(ExecutionStateModel):
    execution_id: str
    status: ReconciliationStatus
    reason_codes: tuple[str, ...] = ()
    record_fingerprint: str


def build_execution_record(intent: ExecutionIntent, *, execution_mode: str, now: datetime) -> ExecutionRecord:
    """Create a deterministic record from exactly one immutable intent."""
    attempt_identity = _fingerprint({
        "proposal_id": intent.proposal_id,
        "proposal_fingerprint": intent.proposal_fingerprint,
        "approval_id": intent.approval_id,
        "account_id": intent.account_id,
    })
    idempotency_key = _fingerprint({
        "attempt_identity": attempt_identity,
        "execution_intent_id": intent.execution_intent_id,
        "execution_intent_fingerprint": intent.fingerprint,
    })
    execution_id = f"execution-{idempotency_key}"
    return ExecutionRecord(
        execution_id=execution_id, idempotency_key=idempotency_key, attempt_identity=attempt_identity,
        execution_intent_id=intent.execution_intent_id, execution_intent_fingerprint=intent.fingerprint,
        proposal_id=intent.proposal_id, proposal_fingerprint=intent.proposal_fingerprint,
        analysis_id=intent.analysis_id, approval_id=intent.approval_id, policy_id=intent.policy_id,
        policy_fingerprint=intent.policy_fingerprint, assessment_id=intent.assessment_id,
        assessment_fingerprint=intent.assessment_fingerprint, evidence_fingerprint=intent.evidence_fingerprint,
        account_id=intent.account_id or "", canonical_symbol=intent.symbol, direction=intent.direction.value,
        quantity=intent.quantity, requested_price=intent.requested_entry, stop_loss=intent.stop_loss,
        take_profit=intent.take_profit, execution_mode=execution_mode, state=ExecutionLifecycleState.INTENT_CREATED,
        created_at=now, updated_at=now, lifecycle_version=1,
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()
