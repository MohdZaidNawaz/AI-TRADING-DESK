"""Non-authoritative contracts for the Phase 13C shared authority bootstrap.

These types describe only schema/bootstrap lifecycle information.  They do not
represent a Sentinel approval, controlled-demo authorization, reservation, or
permission to execute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator

from app.models.schemas import APIModel


class SharedAuthorityMode(str, Enum):
    """The only mode supported by the Phase 13C-1 store."""

    BOOTSTRAP_NON_AUTHORITATIVE = "bootstrap_non_authoritative"


class SharedAuthorityEntityKind(str, Enum):
    """Entity classes reserved by the future shared authority database."""

    SCHEMA_BOOTSTRAP = "schema_bootstrap"
    SENTINEL_APPROVAL = "sentinel_approval"
    DEMO_AUTHORIZATION = "demo_authorization"
    EXECUTION_RESERVATION = "execution_reservation"


class SharedAuthorityModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SharedAuthorityDiagnostics(SharedAuthorityModel):
    """Safe readiness details; deliberately excludes paths and configuration."""

    available: bool
    mode: SharedAuthorityMode = SharedAuthorityMode.BOOTSTRAP_NON_AUTHORITATIVE
    schema_version: str | None = Field(default=None, max_length=32)
    migration_state: str | None = Field(default=None, max_length=64)
    reason_code: str | None = Field(default=None, max_length=120)


class SharedAuthorityBootstrapEvent(SharedAuthorityModel):
    """A safe internal event used only to validate local lifecycle/outbox wiring."""

    event_state: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    occurred_at: datetime
    reason_code: str = Field(min_length=1, max_length=120, pattern=r"^[A-Z0-9_]+$")
    details: tuple[tuple[str, str], ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("shared authority bootstrap timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("details")
    @classmethod
    def validate_safe_details(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        forbidden = ("password", "secret", "token", "api_key", "apikey", "credential")
        normalized: list[tuple[str, str]] = []
        for key, item in value:
            clean_key = key.strip().lower()
            if not clean_key or any(word in clean_key for word in forbidden):
                raise ValueError("shared authority bootstrap details cannot contain sensitive fields")
            normalized.append((clean_key, str(item)))
        return tuple(sorted(set(normalized)))

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "event_state": self.event_state,
                "occurred_at": self.occurred_at.isoformat(),
                "reason_code": self.reason_code,
                "details": self.details,
            }
        )

    @computed_field(return_type=str)
    @property
    def event_id(self) -> str:
        return "shared-authority-bootstrap-" + self.fingerprint


class SharedAuthorityOutboxEvent(SharedAuthorityModel):
    """Immutable, sanitized local outbox record for bootstrap lifecycle events."""

    outbox_id: str = Field(min_length=1, max_length=200)
    lifecycle_event_id: str = Field(min_length=1, max_length=200)
    entity_kind: SharedAuthorityEntityKind
    entity_id: str = Field(min_length=1, max_length=200)
    lifecycle_version: int = Field(ge=1)
    event_state: str = Field(min_length=1, max_length=80)
    reason_code: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    fingerprint: str = Field(min_length=64, max_length=64)

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("shared authority outbox timestamps require timezone awareness")
        return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
