"""Typed, non-authorizing authority-lifecycle protocol contracts.

These contracts describe durable lifecycle operations.  They deliberately do
not turn a caller-supplied model into proof that a Sentinel approval or demo
authorization is genuine.  Phase 13B has no production-authoritative backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from app.models.demo_authorization import ControlledDemoAuthorization
from app.models.durable_authority import DurableAuthorityStatus
from app.models.proposals import SentinelDecision
from app.models.schemas import APIModel


class AuthorityRecordKind(str, Enum):
    APPROVAL = "approval"
    DEMO_AUTHORIZATION = "demo_authorization"


class AuthorityOperation(str, Enum):
    REGISTER = "register"
    VERIFY = "verify"
    REVOKE = "revoke"
    CONSUME = "consume"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AuthorityOperationOutcome(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REJECTED = "rejected"


class AuthorityBackendScope(str, Enum):
    """What an implementation may honestly attest in the current process."""

    DURABLE_LIFECYCLE_ONLY = "durable_lifecycle_only"
    PRODUCTION_AUTHORITATIVE = "production_authoritative"


class AuthorityProtocolModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalAuthorityRequest(AuthorityProtocolModel):
    operation: AuthorityOperation
    decision: SentinelDecision
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_operation(self) -> "ApprovalAuthorityRequest":
        if self.operation not in {
            AuthorityOperation.REGISTER,
            AuthorityOperation.VERIFY,
            AuthorityOperation.CONSUME,
        }:
            raise ValueError("approval requests support register, verify, or consume only")
        return self


class DemoAuthorizationAuthorityRequest(AuthorityProtocolModel):
    operation: AuthorityOperation
    authorization: ControlledDemoAuthorization
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_operation(self) -> "DemoAuthorizationAuthorityRequest":
        if self.operation not in {
            AuthorityOperation.REGISTER,
            AuthorityOperation.VERIFY,
            AuthorityOperation.CONSUME,
        }:
            raise ValueError("demo authorization requests support register, verify, or consume only")
        return self


class AuthorityTransitionRequest(AuthorityProtocolModel):
    record_kind: AuthorityRecordKind
    operation: AuthorityOperation
    external_id: str = Field(min_length=1, max_length=160)
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_operation(self) -> "AuthorityTransitionRequest":
        if self.operation not in {
            AuthorityOperation.REVOKE,
            AuthorityOperation.RECONCILIATION_REQUIRED,
        }:
            raise ValueError("authority transition requests support revoke or reconciliation only")
        return self


class AuthorityOperationResult(AuthorityProtocolModel):
    """Strictly typed lifecycle outcome; never proof of external authority.

    Phase 13B deliberately has no source-trusted production authority backend.
    Consequently, this contract rejects ``PRODUCTION_AUTHORITATIVE`` rather
    than allowing a caller-created result to claim it.  A later reviewed
    migration can introduce a distinct result contract only alongside a genuine
    trusted owner and its verification boundary.
    """

    record_kind: AuthorityRecordKind
    operation: AuthorityOperation
    outcome: AuthorityOperationOutcome
    backend_scope: AuthorityBackendScope
    external_id: str = Field(min_length=1, max_length=160)
    status: DurableAuthorityStatus | None = None
    lifecycle_version: StrictInt | None = Field(default=None, ge=1)
    source_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    expires_at: datetime | None = None
    reason_code: str | None = Field(default=None, max_length=160)

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("authority result identifier must be non-blank and normalized")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "AuthorityOperationResult":
        if self.backend_scope is AuthorityBackendScope.PRODUCTION_AUTHORITATIVE:
            raise ValueError("production-authoritative authority results are unavailable in Phase 13B")

        if self.outcome is AuthorityOperationOutcome.REJECTED:
            if not self.reason_code or not self.reason_code.strip():
                raise ValueError("rejected authority operations require a reason code")
            lineage = (
                self.status,
                self.lifecycle_version,
                self.source_fingerprint,
                self.expires_at,
            )
            if any(value is not None for value in lineage) and any(value is None for value in lineage):
                raise ValueError("rejected authority operations require complete lifecycle lineage when state is known")
            return self

        if self.reason_code is not None:
            raise ValueError("successful authority operations cannot contain a reason code")

        if (
            self.status is None
            or self.lifecycle_version is None
            or self.source_fingerprint is None
            or self.expires_at is None
        ):
            raise ValueError("successful authority operations require complete lifecycle lineage and expiry")

        if self.operation is AuthorityOperation.REGISTER:
            # An idempotent register can truthfully return a terminal record;
            # an applied register must have created an active record.
            if (
                self.outcome is AuthorityOperationOutcome.APPLIED
                and self.status is not DurableAuthorityStatus.ACTIVE
            ):
                raise ValueError("applied registration requires active lifecycle status")
        elif self.operation is AuthorityOperation.VERIFY:
            if (
                self.outcome is not AuthorityOperationOutcome.APPLIED
                or self.status is not DurableAuthorityStatus.ACTIVE
            ):
                raise ValueError("successful verification requires an active applied lifecycle result")
        elif self.operation is AuthorityOperation.CONSUME:
            if self.status is not DurableAuthorityStatus.CONSUMED:
                raise ValueError("successful consumption requires consumed lifecycle status")
        elif self.operation is AuthorityOperation.REVOKE:
            if self.status is not DurableAuthorityStatus.REVOKED:
                raise ValueError("successful revocation requires revoked lifecycle status")
        elif self.operation is AuthorityOperation.RECONCILIATION_REQUIRED:
            if self.status is not DurableAuthorityStatus.RECONCILIATION_REQUIRED:
                raise ValueError("successful reconciliation transition requires reconciliation-required status")
        else:  # Defensive in case the enum gains a new operation.
            raise ValueError("unsupported authority operation")

        return self


@runtime_checkable
class AuthorityLifecycleProtocol(Protocol):
    """Lifecycle API only; conformance is never evidence of trust."""

    @property
    def backend_scope(self) -> AuthorityBackendScope: ...

    def register_approval(self, request: ApprovalAuthorityRequest) -> AuthorityOperationResult: ...

    def verify_approval(self, request: ApprovalAuthorityRequest) -> AuthorityOperationResult: ...

    def consume_approval(self, request: ApprovalAuthorityRequest) -> AuthorityOperationResult: ...

    def register_demo_authorization(
        self, request: DemoAuthorizationAuthorityRequest
    ) -> AuthorityOperationResult: ...

    def verify_demo_authorization(
        self, request: DemoAuthorizationAuthorityRequest
    ) -> AuthorityOperationResult: ...

    def consume_demo_authorization(
        self, request: DemoAuthorizationAuthorityRequest
    ) -> AuthorityOperationResult: ...

    def transition(self, request: AuthorityTransitionRequest) -> AuthorityOperationResult: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authority protocol timestamps require timezone awareness")
    return value.astimezone(UTC)
