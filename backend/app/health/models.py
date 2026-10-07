"""Immutable, credential-free health contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import ConfigDict, Field, FiniteFloat, field_validator

from app.models.schemas import APIModel


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ComponentHealth(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1, max_length=80)
    status: HealthStatus
    checked_at: datetime
    latency_ms: FiniteFloat | None = Field(default=None, ge=0)
    last_success_at: datetime | None = None
    failure_category: str | None = Field(default=None, max_length=80)
    reason_code: str | None = Field(default=None, max_length=120)

    @field_validator("checked_at", "last_success_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("health timestamps require timezone awareness")
        return value.astimezone(UTC)
