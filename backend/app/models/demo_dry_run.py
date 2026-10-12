"""Immutable outcomes for controlled demo dry-run validation.

These contracts document a validation result only.  They deliberately contain
no broker request, adapter response, account credentials, or submission
authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator

from app.models.schemas import APIModel


class DemoDryRunStatus(str, Enum):
    """The only outcomes available to the non-submitting validation boundary."""

    VALIDATED = "validated"
    REJECTED = "rejected"


class ControlledDemoDryRunResult(APIModel):
    """A deterministic, non-authorizing result for one demo validation pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: str | None = Field(default=None, max_length=160)
    authorization_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    proposal_id: str | None = Field(default=None, max_length=120)
    proposal_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    execution_intent_id: str | None = Field(default=None, max_length=160)
    execution_intent_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    status: DemoDryRunStatus
    evaluated_at: datetime
    reason_codes: tuple[str, ...] = ()
    submission_attempted: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_state_mutated: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("demo dry-run timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def deterministic_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator(
        "submission_attempted",
        "submission_allowed",
        "execution_state_mutated",
        mode="before",
    )
    @classmethod
    def require_literal_false(cls, value: object) -> Literal[False]:
        """Reject unsafe flags rather than coercing them to a safe-looking value."""
        if value is not False:
            raise ValueError("controlled demo dry-run safety flags must be exactly False")
        return False

    @computed_field(return_type=str)
    @property
    def dry_run_id(self) -> str:
        """Stable identity for a repeated inspection of the same exact lineage."""
        return "demo-dry-run-" + _fingerprint(self._identity())

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                **self._identity(),
                "status": self.status.value,
                "reason_codes": self.reason_codes,
            }
        )

    def _identity(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "authorization_fingerprint": self.authorization_fingerprint,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "execution_intent_id": self.execution_intent_id,
            "execution_intent_fingerprint": self.execution_intent_fingerprint,
        }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return sha256(encoded).hexdigest()
