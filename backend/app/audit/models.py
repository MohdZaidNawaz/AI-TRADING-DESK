"""Immutable, non-sensitive audit event contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator

from app.models.schemas import APIModel


class AuditEventType(str, Enum):
    EXECUTION_LIFECYCLE = "execution_lifecycle"
    SENTINEL_DECISION = "sentinel_decision"
    EXECUTION_INTENT = "execution_intent"
    RECONCILIATION = "reconciliation"
    KILL_SWITCH = "kill_switch"
    ADAPTER_AVAILABILITY = "adapter_availability"
    FAILURE = "failure"
    RECOVERY = "recovery"


class AuditEvent(APIModel):
    """A safe, immutable reference event; payloads and credentials are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: AuditEventType
    occurred_at: datetime
    execution_id: str | None = Field(default=None, max_length=160)
    proposal_id: str | None = Field(default=None, max_length=120)
    intent_id: str | None = Field(default=None, max_length=160)
    approval_id: str | None = Field(default=None, max_length=160)
    account_reference: str | None = Field(default=None, max_length=160)
    state: str | None = Field(default=None, max_length=80)
    reason_code: str | None = Field(default=None, max_length=120)
    provenance_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    details: tuple[tuple[str, str], ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("details")
    @classmethod
    def _safe_details(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        normalized: list[tuple[str, str]] = []
        forbidden = ("password", "secret", "token", "api_key", "apikey", "credential")
        for key, item in value:
            clean_key = key.strip().lower()
            if not clean_key or any(word in clean_key for word in forbidden):
                raise ValueError("audit details cannot contain sensitive fields")
            normalized.append((clean_key, str(item)))
        return tuple(sorted(set(normalized)))

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self._identity())

    @computed_field(return_type=str)
    @property
    def event_id(self) -> str:
        # The event identity intentionally excludes diagnostic content, so a
        # second write for the same lifecycle point with altered content is a
        # conflict rather than silently becoming a second history entry.
        return "audit-" + _fingerprint(
            {
                "event_type": self.event_type.value,
                "occurred_at": self.occurred_at.isoformat(),
                "execution_id": self.execution_id,
                "proposal_id": self.proposal_id,
                "intent_id": self.intent_id,
                "approval_id": self.approval_id,
                "state": self.state,
                "reason_code": self.reason_code,
            }
        )

    def _identity(self) -> dict[str, object]:
        return {
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "execution_id": self.execution_id,
            "proposal_id": self.proposal_id,
            "intent_id": self.intent_id,
            "approval_id": self.approval_id,
            "account_reference": self.account_reference,
            "state": self.state,
            "reason_code": self.reason_code,
            "provenance_fingerprint": self.provenance_fingerprint,
            "details": self.details,
        }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return sha256(encoded).hexdigest()
