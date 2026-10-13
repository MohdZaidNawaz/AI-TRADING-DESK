"""Immutable contracts for the test-only first demo-order boundary.

These models deliberately describe a synthetic submission boundary, not a
broker request or successful broker outcome.  A later separately approved
adapter would require its own production authorization work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator, model_validator

from app.models.proposals import TradeDirection
from app.models.schemas import APIModel


class DemoSubmissionStatus(str, Enum):
    REJECTED = "rejected"
    TEST_BOUNDARY_REACHED = "test_boundary_reached"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class TestOnlyAdapterOutcome(str, Enum):
    __test__ = False
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CONNECTION_FAILURE = "connection_failure"
    UNKNOWN = "unknown"


class TestOnlyReservationStatus(str, Enum):
    """Lifecycle local to the synthetic boundary; never an execution state."""

    RESERVED = "reserved"
    REJECTED = "rejected"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DemoSubmissionModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FirstDemoTestPermit(DemoSubmissionModel):
    """Explicit immutable permit for tests; it cannot enable real execution."""

    authorization_id: str = Field(min_length=1, max_length=160)
    authorization_fingerprint: str = Field(min_length=64, max_length=64)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    issued_at: datetime
    expires_at: datetime
    scope: Literal["first_demo_xauusd_test_only"] = "first_demo_xauusd_test_only"
    test_only: Literal[True] = True

    @field_validator("issued_at", "expires_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("test permit timestamps require timezone awareness")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_window(self) -> "FirstDemoTestPermit":
        if self.issued_at >= self.expires_at:
            raise ValueError("test permit expiry must follow issuance")
        return self

    @computed_field(return_type=str)
    @property
    def permit_id(self) -> str:
        return "first-demo-test-permit-" + _fingerprint(self._identity())

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self._identity())

    def _identity(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "authorization_fingerprint": self.authorization_fingerprint,
            "execution_intent_id": self.execution_intent_id,
            "execution_intent_fingerprint": self.execution_intent_fingerprint,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "scope": self.scope,
            "test_only": self.test_only,
        }


class FirstDemoOrderRequest(DemoSubmissionModel):
    """Sanitized explicit request passed only to a test-only adapter."""

    canonical_symbol: Literal["XAUUSD"] = "XAUUSD"
    broker_symbol: Literal["XAUUSD"] = "XAUUSD"
    direction: TradeDirection
    quantity: FiniteFloat
    reference_price: FiniteFloat
    stop_loss: FiniteFloat
    take_profit: FiniteFloat
    order_type: Literal["MARKET"] = "MARKET"
    filling_mode: Literal["FOK", "IOC"]
    execution_intent_id: str = Field(min_length=1, max_length=160)
    permit_id: str = Field(min_length=1, max_length=160)

    @field_validator("quantity")
    @classmethod
    def exact_first_order_volume(cls, value: float) -> float:
        if Decimal(str(value)) != Decimal("0.01"):
            raise ValueError("first demo order volume must be exactly 0.01")
        return value


class TestOnlyBoundaryReservation(DemoSubmissionModel):
    """One process-local synthetic boundary reservation, never executable."""

    permit_id: str = Field(min_length=1, max_length=160)
    permit_fingerprint: str = Field(min_length=64, max_length=64)
    authorization_id: str = Field(min_length=1, max_length=160)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    proposal_id: str = Field(min_length=1, max_length=120)
    reserved_at: datetime
    status: TestOnlyReservationStatus = TestOnlyReservationStatus.RESERVED
    test_only: Literal[True] = True

    @field_validator("reserved_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("test reservation timestamps require timezone awareness")
        return value.astimezone(UTC)

    @computed_field(return_type=str)
    @property
    def reservation_id(self) -> str:
        return "first-demo-test-reservation-" + _fingerprint(self._identity())

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self._identity())

    def _identity(self) -> dict[str, object]:
        return {
            "permit_id": self.permit_id,
            "permit_fingerprint": self.permit_fingerprint,
            "authorization_id": self.authorization_id,
            "execution_intent_id": self.execution_intent_id,
            "proposal_id": self.proposal_id,
            "test_only": self.test_only,
        }


class TestOnlyAdapterResponse(DemoSubmissionModel):
    __test__ = False
    outcome: TestOnlyAdapterOutcome
    observed_at: datetime
    reason_code: str | None = Field(default=None, max_length=120)

    @field_validator("observed_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("test adapter timestamps require timezone awareness")
        return value.astimezone(UTC)


class FirstDemoExecutionResult(DemoSubmissionModel):
    """A result for a fake boundary only; it can never claim broker submission."""

    permit_id: str | None = Field(default=None, max_length=160)
    execution_id: str | None = Field(default=None, max_length=160)
    test_reservation_id: str | None = Field(default=None, max_length=180)
    authorization_id: str | None = Field(default=None, max_length=160)
    status: DemoSubmissionStatus
    evaluated_at: datetime
    reason_codes: tuple[str, ...] = ()
    test_boundary_reached: bool = False
    broker_submission_attempted: Literal[False] = False
    submission_allowed: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("first demo execution timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def deterministic_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("broker_submission_attempted", "submission_allowed", mode="before")
    @classmethod
    def require_literal_false(cls, value: object) -> Literal[False]:
        if value is not False:
            raise ValueError("test-only result safety flags must be exactly False")
        return False


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
