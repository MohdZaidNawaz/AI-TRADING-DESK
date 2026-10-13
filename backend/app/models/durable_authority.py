"""Frozen, non-submitting state contracts for Phase 13A.

These records preserve approval and controlled-demo authorization lifecycle
state.  They are not broker requests and do not grant execution authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.demo_authorization import ControlledDemoAuthorization
from app.models.proposals import SentinelDecision, SentinelDecisionStatus
from app.models.schemas import APIModel


class DurableAuthorityStatus(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DurableAuthorityModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DurableApprovalState(DurableAuthorityModel):
    """One registered Sentinel approval and its durable local lifecycle."""

    decision: SentinelDecision
    registered_at: datetime
    updated_at: datetime
    status: DurableAuthorityStatus = DurableAuthorityStatus.ACTIVE
    lifecycle_version: int = Field(default=1, ge=1)

    @field_validator("registered_at", "updated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_approval(self) -> "DurableApprovalState":
        if self.decision.decision is not SentinelDecisionStatus.APPROVED:
            raise ValueError("durable approval state requires a Sentinel-approved decision")
        if not self.decision.approval_id or self.decision.expires_at is None:
            raise ValueError("durable approval state requires approval ID and expiry")
        if self.registered_at > self.updated_at:
            raise ValueError("durable approval update cannot predate registration")
        return self

    @computed_field(return_type=str)
    @property
    def approval_id(self) -> str:
        assert self.decision.approval_id is not None
        return self.decision.approval_id

    @computed_field(return_type=str)
    @property
    def record_id(self) -> str:
        return "durable-approval-" + _fingerprint(
            {"approval_id": self.approval_id, "decision_id": self.decision.decision_id}
        )

    @computed_field(return_type=str)
    @property
    def source_fingerprint(self) -> str:
        return _fingerprint(self.decision.model_dump(mode="json"))

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "record_id": self.record_id,
                "source_fingerprint": self.source_fingerprint,
                "status": self.status.value,
                "lifecycle_version": self.lifecycle_version,
                "registered_at": self.registered_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
            }
        )


class DurableDemoAuthorizationState(DurableAuthorityModel):
    """One issued controlled-demo authorization and durable local lifecycle."""

    authorization: ControlledDemoAuthorization
    issued_at: datetime
    updated_at: datetime
    status: DurableAuthorityStatus = DurableAuthorityStatus.ACTIVE
    lifecycle_version: int = Field(default=1, ge=1)

    @field_validator("issued_at", "updated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_authorization(self) -> "DurableDemoAuthorizationState":
        if self.authorization.status.value != "authorized":
            raise ValueError("durable authorization state requires an issued authorization")
        if self.issued_at > self.updated_at:
            raise ValueError("durable authorization update cannot predate issuance")
        return self

    @computed_field(return_type=str)
    @property
    def authorization_id(self) -> str:
        return self.authorization.authorization_id

    @computed_field(return_type=str)
    @property
    def record_id(self) -> str:
        return "durable-demo-authorization-" + _fingerprint(
            {"authorization_id": self.authorization_id, "intent_id": self.authorization.execution_intent_id}
        )

    @computed_field(return_type=str)
    @property
    def source_fingerprint(self) -> str:
        return self.authorization.fingerprint

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "record_id": self.record_id,
                "source_fingerprint": self.source_fingerprint,
                "status": self.status.value,
                "lifecycle_version": self.lifecycle_version,
                "issued_at": self.issued_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
            }
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("durable authority timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
