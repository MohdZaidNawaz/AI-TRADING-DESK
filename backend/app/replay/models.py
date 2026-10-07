"""Immutable replay references that never contain raw provider payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator

from app.models.schemas import APIModel


class ReplayEvent(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(min_length=1, max_length=80)
    reference_id: str = Field(min_length=1, max_length=160)
    fingerprint: str = Field(min_length=64, max_length=64)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("replay timestamps require timezone awareness")
        return value.astimezone(UTC)

    @computed_field(return_type=str)
    @property
    def event_id(self) -> str:
        return "replay-" + _hash(self.model_dump(mode="json", exclude={"event_id"}))


class ReplayScenario(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1, max_length=160)
    events: tuple[ReplayEvent, ...]

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _hash({"scenario_id": self.scenario_id, "events": [item.model_dump(mode="json") for item in self.ordered_events]})

    @property
    def ordered_events(self) -> tuple[ReplayEvent, ...]:
        return tuple(sorted(self.events, key=lambda item: (item.occurred_at, item.domain, item.reference_id, item.fingerprint)))


class ReplayResult(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    scenario_fingerprint: str
    replayed_event_ids: tuple[str, ...]
    result_fingerprint: str


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
