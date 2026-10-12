"""Immutable, provider-neutral pre-authorization for one future demo order.

This module deliberately describes authorization only.  It cannot submit a
broker order, does not import an execution adapter, and carries no credentials
or provider payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator, model_validator

from app.models.demo_execution import DemoInstrumentBinding
from app.models.portfolio_risk import SourceAccountKey
from app.models.proposals import TradeDirection
from app.models.schemas import APIModel


class DemoAuthorizationStatus(str, Enum):
    """The bounded lifecycle of a non-submittable demo authorization."""

    AUTHORIZED = "authorized"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DemoAuthorizationModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControlledDemoAuthorization(DemoAuthorizationModel):
    """Exact immutable lineage for the first controlled-demo authorization.

    The identity intentionally binds only normalized, non-sensitive contract
    values.  It does not carry broker requests, passwords, provider payloads,
    or an instruction to submit.
    """

    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    account: SourceAccountKey
    instrument: DemoInstrumentBinding
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    analysis_id: str = Field(min_length=1, max_length=120)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    approval_id: str = Field(min_length=1, max_length=160)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64)
    direction: TradeDirection
    quantity: FiniteFloat = Field(gt=0)
    order_type: str = Field(min_length=1, max_length=32)
    filling_mode: str = Field(min_length=1, max_length=32)
    created_at: datetime
    execution_window_start: datetime
    execution_window_end: datetime
    expires_at: datetime
    status: DemoAuthorizationStatus = DemoAuthorizationStatus.AUTHORIZED

    @field_validator(
        "policy_id",
        "policy_version",
        "proposal_id",
        "analysis_id",
        "approval_id",
        "execution_intent_id",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized

    @field_validator("order_type", "filling_mode", mode="before")
    @classmethod
    def normalize_execution_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("created_at", "execution_window_start", "execution_window_end", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("demo authorization timestamps require timezone awareness")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_narrow_scope(self) -> "ControlledDemoAuthorization":
        if self.account.source_id != "mt5" or self.account.account_reference is None:
            raise ValueError("controlled demo authorization requires an exact mt5 account binding")
        if self.order_type != "MARKET":
            raise ValueError("controlled demo authorization permits only MARKET orders")
        if self.filling_mode not in {"FOK", "IOC"}:
            raise ValueError("controlled demo authorization permits only FOK or IOC filling modes")
        if not self.execution_window_start <= self.created_at < self.execution_window_end:
            raise ValueError("authorization creation must occur inside its execution window")
        if self.execution_window_start >= self.execution_window_end:
            raise ValueError("execution window must have positive duration")
        if self.expires_at > self.execution_window_end:
            raise ValueError("authorization expiry cannot exceed its execution window")
        if self.expires_at <= self.created_at:
            raise ValueError("authorization expiry must follow its creation time")
        return self

    @computed_field(return_type=str)
    @property
    def authorization_id(self) -> str:
        return "demo-authorization-" + _fingerprint(self._identity())

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint({**self._identity(), "status": self.status.value})

    def _identity(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "account": self.account.model_dump(mode="json"),
            "instrument": self.instrument.model_dump(mode="json"),
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "analysis_id": self.analysis_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "approval_id": self.approval_id,
            "execution_intent_id": self.execution_intent_id,
            "execution_intent_fingerprint": self.execution_intent_fingerprint,
            "direction": self.direction.value,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "filling_mode": self.filling_mode,
            "created_at": self.created_at.isoformat(),
            "execution_window_start": self.execution_window_start.isoformat(),
            "execution_window_end": self.execution_window_end.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class DemoAuthorizationResult(DemoAuthorizationModel):
    """A deterministic authorization outcome that never grants submission."""

    authorization: ControlledDemoAuthorization | None = None
    evaluated_at: datetime
    reason_codes: tuple[str, ...] = ()
    submission_allowed: bool = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorization result timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def deterministic_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class DemoAuthorizationVerification(DemoAuthorizationModel):
    """Future-facing verification result; verification never consumes authority."""

    authorization: ControlledDemoAuthorization | None = None
    verified_at: datetime
    reason_codes: tuple[str, ...] = ()
    submission_allowed: bool = False

    @field_validator("verified_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorization verification timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def deterministic_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    return sha256(encoded).hexdigest()
