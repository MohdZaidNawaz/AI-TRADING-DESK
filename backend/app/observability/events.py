"""Minimal JSON logging that rejects sensitive keys before logging."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import json
import logging
import re
from typing import Mapping

from pydantic import ConfigDict, Field, field_validator

from app.models.schemas import APIModel


class OperationalSignal(str, Enum):
    """Bounded operational signal names suitable for metrics and alerts."""

    PROVIDER_AVAILABILITY = "provider_availability"
    TRUST_ROOT_STATUS = "trust_root_status"
    LIFECYCLE_STATUS = "lifecycle_status"
    REPLAY_STORE_STATUS = "replay_store_status"
    REPLAY_REJECTION = "replay_rejection"
    ROLLBACK_WITNESS_STATUS = "rollback_witness_status"
    BROKER_EVIDENCE_STATUS = "broker_evidence_status"
    RECONCILIATION_STATUS = "reconciliation_status"
    READINESS_STATUS_CHANGE = "readiness_status_change"
    PROVIDER_CAPACITY = "provider_capacity"
    CONFIGURATION_FAILURE = "configuration_failure"
    RECOVERY_TRANSITION = "recovery_transition"


class OperationalEventState(str, Enum):
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    HEALTHY = "healthy"
    RECOVERED = "recovered"


class OperationalReasonCode(str, Enum):
    CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_CAPACITY_EXHAUSTED = "PROVIDER_CAPACITY_EXHAUSTED"
    TRUST_ROOT_UNAVAILABLE = "TRUST_ROOT_UNAVAILABLE"
    LIFECYCLE_STALE = "LIFECYCLE_STALE"
    REPLAY_STORE_UNAVAILABLE = "REPLAY_STORE_UNAVAILABLE"
    REPLAY_REJECTED = "REPLAY_REJECTED"
    WITNESS_CONFLICT = "WITNESS_CONFLICT"
    BROKER_EVIDENCE_INVALID = "BROKER_EVIDENCE_INVALID"
    RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"
    READINESS_CHANGED = "READINESS_CHANGED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_COMPLETED = "RECOVERY_COMPLETED"


def operational_event(
    *,
    timestamp: datetime,
    signal: OperationalSignal,
    state: OperationalEventState,
    reason_code: OperationalReasonCode,
    severity: str = "WARNING",
) -> "StructuredEvent":
    """Build a bounded, sanitized signal without raw payload or secret fields."""

    if type(signal) is not OperationalSignal:
        raise ValueError("operational event signal must use the bounded vocabulary")
    if type(state) is not OperationalEventState:
        raise ValueError("operational event state must use the bounded vocabulary")
    if type(reason_code) is not OperationalReasonCode:
        raise ValueError("operational event reason code must use the bounded vocabulary")

    return StructuredEvent(
        timestamp=timestamp,
        component="operational_readiness",
        event_type=signal.value,
        severity=severity,
        state=state.value,
        reason_code=reason_code.value,
    )


_SENSITIVE = ("password", "secret", "token", "apikey", "api_key", "credential", "authorization")
_SENSITIVE_VALUE = re.compile(
    r"(?ix)(?:"
    r"[\"']?(?:password|secret|token|api[_-]?key|apikey|credential|authorization)"
    r"[\"']?\s*[:=]\s*\S+"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"
    r"|-----BEGIN\s+(?:[A-Z0-9]+\s+)?PRIVATE\s+KEY-----"
    r")"
)


class StructuredEvent(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    component: str = Field(min_length=1, max_length=80)
    event_type: str = Field(min_length=1, max_length=120)
    severity: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")
    correlation_id: str | None = Field(default=None, max_length=160)
    proposal_id: str | None = Field(default=None, max_length=160)
    analysis_id: str | None = Field(default=None, max_length=160)
    intent_id: str | None = Field(default=None, max_length=160)
    execution_id: str | None = Field(default=None, max_length=160)
    approval_id: str | None = Field(default=None, max_length=160)
    provider: str | None = Field(default=None, max_length=80)
    symbol: str | None = Field(default=None, max_length=80)
    account_reference: str | None = Field(default=None, max_length=160)
    state: str | None = Field(default=None, max_length=80)
    reason_code: str | None = Field(default=None, max_length=120)
    fields: tuple[tuple[str, str], ...] = ()

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("structured log timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator(
        "component",
        "event_type",
        "correlation_id",
        "proposal_id",
        "analysis_id",
        "intent_id",
        "execution_id",
        "approval_id",
        "provider",
        "symbol",
        "account_reference",
        "state",
        "reason_code",
        mode="before",
    )
    @classmethod
    def _sanitize_identifier(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        normalized = " ".join(value.split())
        if not normalized or _SENSITIVE_VALUE.search(normalized):
            return "[REDACTED]"
        return normalized

    @field_validator("fields")
    @classmethod
    def _redact(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        output: list[tuple[str, str]] = []
        for key, item in value:
            normalized = key.strip().lower()
            rendered = str(item)
            if (
                not normalized
                or any(marker in normalized for marker in _SENSITIVE)
                or _SENSITIVE_VALUE.search(rendered)
            ):
                continue
            output.append((normalized, rendered))
        return tuple(sorted(set(output)))


def log_event(event: StructuredEvent, *, logger: logging.Logger | None = None) -> None:
    """Emit a stable safe JSON record; callers cannot supply arbitrary objects."""
    target = logger or logging.getLogger("friday")
    payload = event.model_dump(mode="json")
    target.log(getattr(logging, event.severity), json.dumps(payload, sort_keys=True, separators=(",", ":")))


def safe_fields(values: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Convert only safe scalar metadata into a frozen logging representation."""
    return tuple((str(key), str(value)) for key, value in values.items())
