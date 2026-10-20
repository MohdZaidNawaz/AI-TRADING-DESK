"""Minimal JSON logging that rejects sensitive keys before logging."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re
from typing import Mapping

from pydantic import ConfigDict, Field, field_validator

from app.models.schemas import APIModel


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
