"""Immutable, non-authorizing personal trust bootstrap contracts.

These models separate local key custody and a human's local fingerprint
confirmation from independently authenticated key-status authority.  Model
construction, local confirmation, and structurally valid observations never
create production or execution authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.independent_governance import ExternalAuthorityAvailability
from app.models.personal_policy_signing import (
    PersonalKeyRecoveryStatus,
    PersonalPublicKeyDescriptor,
)
from app.models.schemas import APIModel


class PersonalTrustBootstrapModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PersonalTrustDecisionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class PersonalTrustVerificationMethod(str, Enum):
    OUT_OF_BAND_FINGERPRINT = "out_of_band_fingerprint"
    LOCAL_KEY_SIGNATURE = "local_key_signature"
    LOCAL_SELF_AUTHORED_MANIFEST = "local_self_authored_manifest"


class PersonalKeyAuthorityState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    RECOVERY_REQUIRED = "recovery_required"


class PersonalKeyAuthorityAssessmentStatus(str, Enum):
    STRUCTURALLY_VALIDATED = "structurally_validated"
    REJECTED = "rejected"
    STALE = "stale"
    REPLAYED = "replayed"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    RECOVERY_REQUIRED = "recovery_required"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class PersonalAuthorityReplayStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    ROLLBACK = "rollback"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class PersonalTrustBootstrapStatus(str, Enum):
    PENDING_HUMAN_CONFIRMATION = "pending_human_confirmation"
    LOCAL_CONFIRMATION_RECORDED = "local_confirmation_recorded"
    REJECTED = "rejected"
    INVALID = "invalid"


class PersonalRecoveryState(str, Enum):
    LOCAL_KEY_AVAILABLE = "local_key_available"
    KEY_LOST = "key_lost"
    KEY_CORRUPT_OR_UNDECRYPTABLE = "key_corrupt_or_undecryptable"
    SUSPECTED_COMPROMISE = "suspected_compromise"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    AUTHORITY_RECOVERY_REQUIRED = "authority_recovery_required"
    INTERRUPTED = "interrupted"
    ROLLBACK_DETECTED = "rollback_detected"


class PersonalTrustScope(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-trust-scope/v1"] = "personal-trust-scope/v1"
    scope_id: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=160)
    execution_mode: str = Field(min_length=1, max_length=32)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    risk_currency: str = Field(min_length=3, max_length=12)
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)

    @field_validator(
        "scope_id",
        "account_id",
        "execution_mode",
        "canonical_symbol",
        "risk_currency",
        "policy_id",
        "policy_version",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class PersonalTrustBootstrapCeremony(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-trust-bootstrap-ceremony/v2"] = (
        "personal-trust-bootstrap-ceremony/v2"
    )
    ceremony_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    descriptor: PersonalPublicKeyDescriptor
    expected_public_key_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    scope: PersonalTrustScope
    expected_authority_id: str = Field(min_length=1, max_length=160)
    trust_anchor_id: str = Field(min_length=1, max_length=160)
    trust_anchor_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
    )
    trust_anchor_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    presented_at: datetime
    expires_at: datetime
    required_verification_method: Literal[
        PersonalTrustVerificationMethod.OUT_OF_BAND_FINGERPRINT
    ] = PersonalTrustVerificationMethod.OUT_OF_BAND_FINGERPRINT
    local_key_self_trust_allowed: Literal[False] = False
    self_authored_manifest_accepted: Literal[False] = False
    authorizes_execution: Literal[False] = False

    @field_validator("expected_authority_id", "trust_anchor_id", "trust_anchor_version", mode="before")
    @classmethod
    def normalize_trust_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("presented_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_ceremony(self) -> "PersonalTrustBootstrapCeremony":
        if self.expected_public_key_fingerprint != self.descriptor.public_key_fingerprint:
            raise ValueError("bootstrap ceremony must present the exact public-key fingerprint")
        if self.expires_at <= self.presented_at:
            raise ValueError("bootstrap ceremony expiry must follow presentation")
        if self.descriptor.expires_at is not None and self.expires_at > self.descriptor.expires_at:
            raise ValueError("bootstrap ceremony cannot outlive the signing key")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class PersonalTrustDecision(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-trust-decision/v2"] = "personal-trust-decision/v2"
    decision_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    ceremony_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_id: str = Field(min_length=1, max_length=160)
    public_key_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    authority_id: str = Field(min_length=1, max_length=160)
    trust_anchor_id: str = Field(min_length=1, max_length=160)
    trust_anchor_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
    )
    trust_anchor_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    ceremony_presented_at: datetime
    ceremony_expires_at: datetime
    status: PersonalTrustDecisionStatus
    verification_method: PersonalTrustVerificationMethod | None = None
    decided_at: datetime | None = None
    decision_reference: str | None = Field(default=None, min_length=1, max_length=200)
    reason_codes: tuple[str, ...] = ()
    independently_established: Literal[False] = False
    production_authorization_available: Literal[False] = False

    @field_validator("key_id", "authority_id", "trust_anchor_id", "trust_anchor_version", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("ceremony_presented_at", "ceremony_expires_at", "decided_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("decision_reference", mode="before")
    @classmethod
    def safe_reference(cls, value: object) -> object:
        return _safe_reference(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_decision(self) -> "PersonalTrustDecision":
        if self.ceremony_expires_at <= self.ceremony_presented_at:
            raise ValueError("trust decision ceremony window is invalid")
        if self.status is PersonalTrustDecisionStatus.PENDING:
            if any((self.verification_method, self.decided_at, self.decision_reference)):
                raise ValueError("pending trust decisions cannot contain completed decision facts")
            if not self.reason_codes:
                raise ValueError("pending trust decisions require a reason")
        elif self.status is PersonalTrustDecisionStatus.CONFIRMED:
            if self.verification_method is not PersonalTrustVerificationMethod.OUT_OF_BAND_FINGERPRINT:
                raise ValueError("confirmed trust requires out-of-band fingerprint verification")
            if self.decided_at is None or self.decision_reference is None or self.reason_codes:
                raise ValueError("confirmed trust requires decision provenance and no failures")
        elif self.decided_at is None or self.decision_reference is None or not self.reason_codes:
            raise ValueError("rejected trust requires decision provenance and reasons")
        if self.decided_at is not None and not (
            self.ceremony_presented_at <= self.decided_at < self.ceremony_expires_at
        ):
            raise ValueError("trust decision time must fall within the ceremony window")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class PersonalKeyAuthorityRequest(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-key-authority-request/v2"] = (
        "personal-key-authority-request/v2"
    )
    authority_id: str = Field(min_length=1, max_length=160)
    trust_anchor_id: str = Field(min_length=1, max_length=160)
    trust_anchor_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
    )
    trust_anchor_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_id: str = Field(min_length=1, max_length=160)
    public_key_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_generation: int = Field(ge=1)
    scope: PersonalTrustScope
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)
    minimum_source_sequence: int | None = Field(default=None, ge=0)

    @field_validator("authority_id", "trust_anchor_id", "trust_anchor_version", "key_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_scope(self) -> "PersonalKeyAuthorityRequest":
        if self.scope_fingerprint != self.scope.fingerprint:
            raise ValueError("authority request must bind the exact personal scope")
        return self

    @computed_field(return_type=str)
    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"request_fingerprint"}))


class PersonalKeyAuthorityObservation(PersonalTrustBootstrapModel):
    """Sanitized provider claim; never proof of independent authority by itself."""

    schema_version: Literal["personal-key-authority-observation/v2"] = (
        "personal-key-authority-observation/v2"
    )
    availability: ExternalAuthorityAvailability
    provider_id: str = Field(min_length=1, max_length=160)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    authority_id: str | None = Field(default=None, min_length=1, max_length=160)
    trust_anchor_id: str | None = Field(default=None, min_length=1, max_length=160)
    trust_anchor_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$",
    )
    trust_anchor_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_id: str | None = Field(default=None, min_length=1, max_length=160)
    public_key_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_generation: int | None = Field(default=None, ge=1)
    scope: PersonalTrustScope | None = None
    scope_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_state: PersonalKeyAuthorityState | None = None
    replacement_key_id: str | None = Field(default=None, min_length=1, max_length=160)
    replacement_key_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replacement_generation: int | None = Field(default=None, ge=2)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, gt=0)
    observed_at: datetime | None = None
    retrieved_at: datetime
    expires_at: datetime | None = None
    audit_reference: str | None = Field(default=None, min_length=1, max_length=200)
    source_authenticated: bool = False
    trust_anchor_validated: bool = False
    reason_codes: tuple[str, ...] = ()
    authorizes_execution: Literal[False] = False

    @field_validator(
        "provider_id",
        "authority_id",
        "trust_anchor_id",
        "trust_anchor_version",
        "key_id",
        "replacement_key_id",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "retrieved_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("source_event_id", "audit_reference", mode="before")
    @classmethod
    def safe_reference(cls, value: object) -> object:
        return _safe_reference(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_observation(self) -> "PersonalKeyAuthorityObservation":
        facts = (
            self.authority_id,
            self.trust_anchor_id,
            self.trust_anchor_version,
            self.trust_anchor_fingerprint,
            self.key_id,
            self.public_key_fingerprint,
            self.key_generation,
            self.scope,
            self.scope_fingerprint,
            self.key_state,
            self.source_event_id,
            self.source_sequence,
            self.observed_at,
            self.expires_at,
            self.audit_reference,
        )
        if self.availability is ExternalAuthorityAvailability.AVAILABLE:
            if any(value is None for value in facts) or self.reason_codes:
                raise ValueError("available key-status observations require complete facts")
            assert self.observed_at is not None and self.expires_at is not None
            if self.retrieved_at < self.observed_at or self.expires_at <= self.observed_at:
                raise ValueError("key-status observation timestamps are invalid")
            assert self.scope is not None and self.scope_fingerprint is not None
            if self.scope_fingerprint != self.scope.fingerprint:
                raise ValueError("key-status observation must bind the exact personal scope")
            replacement = (
                self.replacement_key_id,
                self.replacement_key_fingerprint,
                self.replacement_generation,
            )
            if self.key_state is PersonalKeyAuthorityState.SUPERSEDED:
                if any(value is None for value in replacement):
                    raise ValueError("superseded keys require complete replacement lineage")
                if self.replacement_key_id == self.key_id:
                    raise ValueError("replacement key identity must be distinct")
                if self.replacement_generation is not None and self.key_generation is not None:
                    if self.replacement_generation <= self.key_generation:
                        raise ValueError("replacement generation must advance")
            elif any(value is not None for value in replacement):
                raise ValueError("only superseded keys may include replacement lineage")
        elif any(value is not None for value in facts) or any(
            value is not None
            for value in (
                self.replacement_key_id,
                self.replacement_key_fingerprint,
                self.replacement_generation,
            )
        ) or self.source_authenticated or self.trust_anchor_validated or not self.reason_codes:
            raise ValueError("unavailable key-status observations require only failure details")
        return self

    @computed_field(return_type=str)
    @property
    def observation_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"observation_fingerprint"}))


class PersonalAuthorityReplayResult(PersonalTrustBootstrapModel):
    status: PersonalAuthorityReplayStatus
    provider_id: str = Field(min_length=1, max_length=160)
    authority_id: str = Field(min_length=1, max_length=160)
    key_id: str = Field(min_length=1, max_length=160)
    source_event_id: str = Field(min_length=1, max_length=200)
    source_sequence: int = Field(gt=0)
    observation_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    checked_at: datetime
    reason_codes: tuple[str, ...] = ()

    @field_validator("provider_id", "authority_id", "key_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("checked_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_result(self) -> "PersonalAuthorityReplayResult":
        if self.status is PersonalAuthorityReplayStatus.ACCEPTED:
            if self.reason_codes:
                raise ValueError("accepted replay checks cannot contain failures")
        elif not self.reason_codes:
            raise ValueError("failed replay checks require reasons")
        return self


class PersonalKeyAuthorityAssessment(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-key-authority-assessment/v2"] = (
        "personal-key-authority-assessment/v2"
    )
    status: PersonalKeyAuthorityAssessmentStatus
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1, max_length=160)
    authority_id: str = Field(min_length=1, max_length=160)
    trust_anchor_id: str = Field(min_length=1, max_length=160)
    trust_anchor_version: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
    )
    trust_anchor_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_id: str = Field(min_length=1, max_length=160)
    public_key_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_generation: int = Field(ge=1)
    scope: PersonalTrustScope
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)
    minimum_source_sequence: int | None = Field(default=None, ge=0)
    evaluated_at: datetime
    observation_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, gt=0)
    observed_at: datetime | None = None
    retrieved_at: datetime | None = None
    observation_expires_at: datetime | None = None
    key_state: PersonalKeyAuthorityState | None = None
    replacement_key_id: str | None = Field(default=None, min_length=1, max_length=160)
    replacement_key_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replacement_generation: int | None = Field(default=None, ge=2)
    reason_codes: tuple[str, ...] = ()
    source_authentication_claim_validated: bool = False
    replay_check_passed: bool = False
    independently_authoritative: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator(
        "provider_id",
        "authority_id",
        "trust_anchor_id",
        "trust_anchor_version",
        "key_id",
        "replacement_key_id",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("requested_at", "evaluated_at", "observed_at", "retrieved_at", "observation_expires_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "PersonalKeyAuthorityAssessment":
        request = PersonalKeyAuthorityRequest(
            authority_id=self.authority_id,
            trust_anchor_id=self.trust_anchor_id,
            trust_anchor_version=self.trust_anchor_version,
            trust_anchor_fingerprint=self.trust_anchor_fingerprint,
            key_id=self.key_id,
            public_key_fingerprint=self.public_key_fingerprint,
            key_generation=self.key_generation,
            scope=self.scope,
            scope_fingerprint=self.scope_fingerprint,
            challenge_id=self.challenge_id,
            requested_at=self.requested_at,
            max_age_seconds=self.max_age_seconds,
            minimum_source_sequence=self.minimum_source_sequence,
        )
        if self.request_fingerprint != request.request_fingerprint:
            raise ValueError("authority assessment request lineage is inconsistent")
        lineage = (
            self.observation_fingerprint,
            self.source_event_id,
            self.source_sequence,
            self.observed_at,
            self.retrieved_at,
            self.observation_expires_at,
        )
        validated_statuses = {
            PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED,
            PersonalKeyAuthorityAssessmentStatus.REVOKED,
            PersonalKeyAuthorityAssessmentStatus.SUPERSEDED,
            PersonalKeyAuthorityAssessmentStatus.COMPROMISED,
            PersonalKeyAuthorityAssessmentStatus.RECOVERY_REQUIRED,
        }
        if self.status in validated_statuses:
            if any(value is None for value in lineage) or self.key_state is None:
                raise ValueError("validated key-status assessments require complete lineage")
            if not self.source_authentication_claim_validated or not self.replay_check_passed:
                raise ValueError("validated key status requires authentication and replay checks")
            expected = {
                PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED: PersonalKeyAuthorityState.ACTIVE,
                PersonalKeyAuthorityAssessmentStatus.REVOKED: PersonalKeyAuthorityState.REVOKED,
                PersonalKeyAuthorityAssessmentStatus.SUPERSEDED: PersonalKeyAuthorityState.SUPERSEDED,
                PersonalKeyAuthorityAssessmentStatus.COMPROMISED: PersonalKeyAuthorityState.COMPROMISED,
                PersonalKeyAuthorityAssessmentStatus.RECOVERY_REQUIRED: PersonalKeyAuthorityState.RECOVERY_REQUIRED,
            }[self.status]
            if self.key_state is not expected:
                raise ValueError("key-status assessment does not match authority state")
            if self.status is PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED:
                if self.reason_codes:
                    raise ValueError("active structural validation cannot contain failures")
            elif not self.reason_codes:
                raise ValueError("terminal key-status assessments require reasons")
        elif not self.reason_codes:
            raise ValueError("failed key-status assessments require reasons")
        if self.status is PersonalKeyAuthorityAssessmentStatus.SUPERSEDED:
            if any(
                value is None
                for value in (
                    self.replacement_key_id,
                    self.replacement_key_fingerprint,
                    self.replacement_generation,
                )
            ):
                raise ValueError("superseded assessments require complete replacement lineage")
            if self.replacement_generation is not None and self.replacement_generation <= self.key_generation:
                raise ValueError("replacement generation must advance")
        elif any(
            value is not None
            for value in (
                self.replacement_key_id,
                self.replacement_key_fingerprint,
                self.replacement_generation,
            )
        ):
            raise ValueError("only superseded assessments may name replacement lineage")
        return self


class PersonalTrustBootstrapAssessment(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-trust-bootstrap-assessment/v1"] = (
        "personal-trust-bootstrap-assessment/v1"
    )
    status: PersonalTrustBootstrapStatus
    ceremony_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    key_id: str = Field(min_length=1, max_length=160)
    public_key_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    decision_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    local_confirmation_recorded: bool = False
    local_key_available: bool = False
    authority_assessment_status: PersonalKeyAuthorityAssessmentStatus | None = None
    reason_codes: tuple[str, ...]
    required_actions: tuple[str, ...]
    independent_trust_established: Literal[False] = False
    authoritative_revocation_established: Literal[False] = False
    replay_rollback_protection_established: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", "required_actions", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "PersonalTrustBootstrapAssessment":
        if not self.reason_codes or not self.required_actions:
            raise ValueError("non-authorizing bootstrap assessments require blockers and actions")
        if self.local_confirmation_recorded and self.decision_fingerprint is None:
            raise ValueError("local confirmation requires decision lineage")
        if self.status is PersonalTrustBootstrapStatus.LOCAL_CONFIRMATION_RECORDED:
            if not self.local_confirmation_recorded:
                raise ValueError("local confirmation status requires a confirmed decision")
        elif self.local_confirmation_recorded:
            raise ValueError("only local-confirmation status may record confirmation")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class PersonalKeyRecoveryAssessment(PersonalTrustBootstrapModel):
    schema_version: Literal["personal-key-recovery-assessment/v1"] = (
        "personal-key-recovery-assessment/v1"
    )
    state: PersonalRecoveryState
    key_id: str = Field(min_length=1, max_length=160)
    evaluated_at: datetime
    local_recovery: PersonalKeyRecoveryStatus
    authority_assessment_status: PersonalKeyAuthorityAssessmentStatus | None = None
    reason_codes: tuple[str, ...]
    required_actions: tuple[str, ...]
    automatic_key_replacement_allowed: Literal[False] = False
    revoked_key_reactivation_allowed: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", "required_actions", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def validate_assessment(self) -> "PersonalKeyRecoveryAssessment":
        if self.key_id != self.local_recovery.key_id:
            raise ValueError("recovery assessment must bind the exact local key")
        if not self.reason_codes or not self.required_actions:
            raise ValueError("recovery assessments require reasons and explicit actions")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


_SENSITIVE_MARKERS = ("password", "private_key", "secret=", "token=", "credential=")


def _safe_reference(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if any(marker in normalized.lower() for marker in _SENSITIVE_MARKERS):
        raise ValueError("references cannot contain sensitive material")
    return normalized


def _reason_codes(value: object) -> object:
    if not isinstance(value, (tuple, list)):
        return value
    normalized = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
    if any(re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None for item in normalized):
        raise ValueError("reason codes must be canonical bounded identifiers")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("personal trust timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
