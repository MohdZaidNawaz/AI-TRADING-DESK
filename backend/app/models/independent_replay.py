"""Immutable diagnostics for local independent-authority replay storage.

The contracts describe local SQLite guarantees only.  They cannot claim an
independent witness, privileged-host rollback resistance, or execution
authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class IndependentReplayModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IndependentReplayStoreStatus(str, Enum):
    READY = "ready"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    MIGRATION_INCOMPLETE = "migration_incomplete"


class IndependentReplayStoreDiagnostics(IndependentReplayModel):
    schema_version: Literal["independent-replay-diagnostics/v1"] = (
        "independent-replay-diagnostics/v1"
    )
    status: IndependentReplayStoreStatus
    checked_at: datetime
    database_schema_version: int | None = Field(default=None, ge=1)
    store_id: str | None = Field(
        default=None, min_length=32, max_length=64, pattern=r"^[0-9a-f]+$"
    )
    reason_codes: tuple[str, ...] = ()
    durable_across_restart: bool = False
    atomic_consumption: bool = False
    external_rollback_resistance: Literal[False] = False
    independently_authoritative: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("checked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("replay diagnostics require timezone-aware timestamps")
        return value.astimezone(UTC)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_reason_codes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        normalized = tuple(
            sorted({str(item).strip().upper() for item in value if str(item).strip()})
        )
        if any(re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None for item in normalized):
            raise ValueError("replay diagnostic reasons must be canonical identifiers")
        return normalized

    @model_validator(mode="after")
    def validate_assurance(self) -> "IndependentReplayStoreDiagnostics":
        if self.status is IndependentReplayStoreStatus.READY:
            if self.reason_codes:
                raise ValueError("ready replay diagnostics cannot contain failures")
            if self.database_schema_version is None or self.store_id is None:
                raise ValueError("ready replay diagnostics require database lineage")
            if not self.durable_across_restart or not self.atomic_consumption:
                raise ValueError("ready replay diagnostics require local durability and atomicity")
        else:
            if not self.reason_codes:
                raise ValueError("unready replay diagnostics require failure reasons")
            if self.durable_across_restart or self.atomic_consumption:
                raise ValueError("unready replay diagnostics cannot advertise assurances")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(mode="json", exclude={"fingerprint"}),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
