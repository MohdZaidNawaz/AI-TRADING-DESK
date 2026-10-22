"""Immutable, non-authorizing production-readiness diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class ProductionAuthorizationModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductionReadinessPrerequisite(str, Enum):
    LOCAL_KEY_CUSTODY = "local_key_custody"
    AUTHORIZED_POLICY_APPROVER = "authorized_policy_approver"
    INDEPENDENT_TRUST_ANCHOR = "independent_trust_anchor"
    INDEPENDENT_ANCHOR_LIFECYCLE = "independent_anchor_lifecycle"
    PERSONAL_TRUST_BOOTSTRAP = "personal_trust_bootstrap"
    PERSONAL_KEY_STATUS_AUTHORITY = "personal_key_status_authority"
    PERSONAL_REPLAY_ROLLBACK_PROTECTION = "personal_replay_rollback_protection"
    INDEPENDENT_REPLAY_STORAGE = "independent_replay_storage"
    ATOMIC_REPLAY_CONSUMPTION = "atomic_replay_consumption"
    EXTERNAL_ROLLBACK_ASSURANCE = "external_rollback_assurance"
    AUTHORITATIVE_APPROVAL_SOURCE = "authoritative_approval_source"
    AUTHORITATIVE_REVOCATION_SOURCE = "authoritative_revocation_source"
    POLICY_INTEGRITY = "policy_integrity"
    TRUSTED_EXECUTION_EVIDENCE = "trusted_execution_evidence"
    SENTINEL_RISK_VERIFICATION = "sentinel_risk_verification"
    SENTINEL_TRADE_APPROVAL = "sentinel_trade_approval"
    DURABLE_RESERVATION = "durable_reservation"
    AUDIT_DURABILITY = "audit_durability"
    KILL_SWITCH = "kill_switch"
    BROKER_RECONCILIATION = "broker_reconciliation"
    SUBMISSION_BOUNDARY_REVIEW = "submission_boundary_review"
    EXPLICIT_EXECUTION_AUTHORIZATION = "explicit_execution_authorization"
    EXECUTION_MODE = "execution_mode"


class ProductionReadinessCheckStatus(str, Enum):
    SATISFIED = "satisfied"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"
    NOT_REVIEWED = "not_reviewed"


class ProductionReadinessCheck(ProductionAuthorizationModel):
    prerequisite: ProductionReadinessPrerequisite
    status: ProductionReadinessCheckStatus
    reason_codes: tuple[str, ...] = ()
    source_reference: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    checked_at: datetime
    authorizes_execution: Literal[False] = False

    @field_validator("checked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("production readiness timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", code) is None
            for code in normalized
        ):
            raise ValueError("production readiness reason codes must be canonical")
        return normalized

    @field_validator("source_reference")
    @classmethod
    def safe_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("production readiness references must be non-empty printable text")
        if re.search(
            r"(?i)(api[_-]?key|password|secret|token|credential|private[_-]?key)\s*[:=]",
            normalized,
        ):
            raise ValueError("production readiness references cannot contain secrets")
        return normalized

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProductionReadinessCheck":
        if self.status is ProductionReadinessCheckStatus.SATISFIED:
            if self.reason_codes:
                raise ValueError("satisfied readiness checks cannot contain failure codes")
            if self.source_reference is None:
                raise ValueError("satisfied readiness checks require source provenance")
        elif not self.reason_codes:
            raise ValueError("unsatisfied readiness checks require reason codes")
        return self


class ProductionAuthorizationReadinessReport(ProductionAuthorizationModel):
    """Complete diagnostic report that is structurally unable to authorize."""

    schema_version: Literal["production-authorization-readiness/v1"] = (
        "production-authorization-readiness/v1"
    )
    status: Literal["blocked"] = "blocked"
    evaluated_at: datetime
    checks: tuple[ProductionReadinessCheck, ...]
    reason_codes: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("production readiness timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def complete_non_authorizing_report(self) -> "ProductionAuthorizationReadinessReport":
        expected = set(ProductionReadinessPrerequisite)
        actual = [item.prerequisite for item in self.checks]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("production readiness report requires every prerequisite once")
        if tuple(actual) != tuple(sorted(actual, key=lambda item: item.value)):
            raise ValueError("production readiness checks require deterministic ordering")
        if any(item.checked_at != self.evaluated_at for item in self.checks):
            raise ValueError("production readiness checks must share one evaluation timestamp")
        failures = {
            code
            for item in self.checks
            if item.status is not ProductionReadinessCheckStatus.SATISFIED
            for code in item.reason_codes
        }
        if not failures or set(self.reason_codes) != failures:
            raise ValueError("production readiness reasons must exactly summarize failed checks")
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
                default=str,
            ).encode()
        ).hexdigest()
