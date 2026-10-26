"""Provider-neutral contracts for external trust attestations.

These immutable contracts validate signed structure and lineage.  They cannot
establish independent trust because this repository has no independently
provisioned verification root or operational external authority.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


class ExternalTrustModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalTrustCapability(str, Enum):
    ROOT_PROVISIONING = "root_provisioning"
    LIFECYCLE_STATUS = "lifecycle_status"
    BROKER_EVIDENCE_AUTHENTICATION = "broker_evidence_authentication"
    ROLLBACK_WITNESS = "rollback_witness"
    AUTHORITATIVE_RECONCILIATION = "authoritative_reconciliation"


class ExternalTrustLifecycle(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"
    RECOVERY_REQUIRED = "recovery_required"


class ExternalReconciliationStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"
    UNRESOLVED = "unresolved"


class ExternalTrustIntegrationStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    CONFIGURED_UNAUTHENTICATED = "configured_unauthenticated"


class BrokerEvidenceLineage(ExternalTrustModel):
    """Signed identity of one broker snapshot and reconciliation observation."""

    evidence_provider_id: str = Field(min_length=1, max_length=160)
    evidence_request_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    evidence_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    account_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    snapshot_observed_at: datetime
    snapshot_retrieved_at: datetime
    reconciliation_id: str = Field(min_length=1, max_length=160)
    reconciliation_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reconciliation_status: Literal["matched"] = "matched"

    @field_validator("evidence_provider_id", "reconciliation_id", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("snapshot_observed_at", "snapshot_retrieved_at")
    @classmethod
    def normalize_snapshot_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_snapshot_chronology(self) -> "BrokerEvidenceLineage":
        if self.snapshot_observed_at > self.snapshot_retrieved_at:
            raise ValueError("broker evidence snapshot chronology is inconsistent")
        return self


class RollbackWitnessLineage(ExternalTrustModel):
    """Canonical checkpoint identity signed by a future external witness."""

    replay_store_id: str = Field(min_length=32, max_length=64, pattern=r"^[0-9a-f]+$")
    replay_stream_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    sequence_head: int = Field(gt=0)
    history_checkpoint_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    previous_checkpoint_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    witness_sequence: int = Field(gt=0)
    update_idempotency_key: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    update_outcome: Literal["confirmed", "unknown", "conflict"]

    @computed_field(return_type=str)
    @property
    def checkpoint_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"checkpoint_fingerprint"}))


class AuthoritativeReconciliationLineage(ExternalTrustModel):
    """Exact external reconciliation subject; it remains non-authorizing locally."""

    account_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    scope_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    evidence_request_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    evidence_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    snapshot_generation: int = Field(gt=0)
    reservation_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    execution_history_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    replay_checkpoint_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reconciliation_generation: int = Field(gt=0)
    coverage_complete: bool
    record_count: int = Field(ge=0)
    status: Literal["matched", "missing", "duplicate", "conflict", "unresolved"]

    @model_validator(mode="after")
    def validate_coverage(self) -> "AuthoritativeReconciliationLineage":
        if self.status == "matched" and not self.coverage_complete:
            raise ValueError("matched reconciliation requires complete coverage")
        if self.status != "matched" and self.coverage_complete:
            raise ValueError("failed reconciliation cannot claim complete coverage")
        return self

    @computed_field(return_type=str)
    @property
    def reconciliation_fingerprint(self) -> str:
        return _fingerprint(
            self.model_dump(mode="json", exclude={"reconciliation_fingerprint"})
        )


class ExternalTrustAttestationRequest(ExternalTrustModel):
    schema_version: Literal["external-trust-attestation-request/v1"] = (
        "external-trust-attestation-request/v1"
    )
    request_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    expected_provider_id: str = Field(min_length=1, max_length=160)
    capability: ExternalTrustCapability
    subject_id: str = Field(min_length=1, max_length=160)
    subject_generation: int = Field(ge=1)
    subject_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=80)
    minimum_source_sequence: int = Field(ge=0)
    requested_at: datetime
    max_age_seconds: int = Field(gt=0, le=3600)
    broker_lineage: BrokerEvidenceLineage | None = None
    witness_lineage: RollbackWitnessLineage | None = None
    reconciliation_lineage: AuthoritativeReconciliationLineage | None = None

    @field_validator("expected_provider_id", "subject_id", "policy_version", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("requested_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @computed_field(return_type=str)
    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"request_fingerprint"}))

    @model_validator(mode="after")
    def validate_capability_context(self) -> "ExternalTrustAttestationRequest":
        contexts = (
            self.broker_lineage,
            self.witness_lineage,
            self.reconciliation_lineage,
        )
        if self.capability is ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION:
            if self.broker_lineage is None:
                raise ValueError("broker evidence requests require exact snapshot lineage")
            if (
                self.subject_fingerprint != self.broker_lineage.evidence_fingerprint
                or self.scope_fingerprint != self.broker_lineage.evidence_request_fingerprint
            ):
                raise ValueError("broker evidence request subject and scope must bind the exact snapshot")
            if self.witness_lineage is not None or self.reconciliation_lineage is not None:
                raise ValueError("broker evidence requests cannot mix capability contexts")
        elif self.capability is ExternalTrustCapability.ROLLBACK_WITNESS:
            if self.witness_lineage is None:
                raise ValueError("rollback witness requests require exact checkpoint lineage")
            if (
                self.subject_fingerprint != self.witness_lineage.checkpoint_fingerprint
                or self.scope_fingerprint != self.witness_lineage.replay_stream_fingerprint
            ):
                raise ValueError("rollback witness request must bind the exact checkpoint")
            if self.broker_lineage is not None or self.reconciliation_lineage is not None:
                raise ValueError("rollback witness requests cannot mix capability contexts")
        elif self.capability is ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION:
            if self.reconciliation_lineage is None:
                raise ValueError("reconciliation requests require exact reconciliation lineage")
            if (
                self.subject_fingerprint
                != self.reconciliation_lineage.reconciliation_fingerprint
                or self.scope_fingerprint != self.reconciliation_lineage.scope_fingerprint
            ):
                raise ValueError("reconciliation request must bind the exact result")
            if self.broker_lineage is not None or self.witness_lineage is not None:
                raise ValueError("reconciliation requests cannot mix capability contexts")
        elif any(context is not None for context in contexts):
            raise ValueError("governance requests cannot contain operational lineage")
        return self


class ExternalTrustAttestationStatement(ExternalTrustModel):
    schema_version: Literal["external-trust-attestation-statement/v1"] = (
        "external-trust-attestation-statement/v1"
    )
    statement_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    provider_id: str = Field(min_length=1, max_length=160)
    verification_authority_id: str = Field(min_length=1, max_length=160)
    verification_root_id: str = Field(min_length=1, max_length=160)
    verification_root_version: str = Field(min_length=1, max_length=80)
    verification_root_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    signature_algorithm: Literal["ED25519"] = "ED25519"
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(min_length=32, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    capability: ExternalTrustCapability
    subject_id: str = Field(min_length=1, max_length=160)
    subject_generation: int = Field(ge=1)
    subject_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=80)
    source_sequence: int = Field(gt=0)
    issued_at: datetime
    observed_at: datetime
    retrieved_at: datetime
    expires_at: datetime
    lifecycle: ExternalTrustLifecycle | None = None
    reconciliation_status: ExternalReconciliationStatus = ExternalReconciliationStatus.NOT_APPLICABLE
    evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    broker_lineage: BrokerEvidenceLineage | None = None
    witness_lineage: RollbackWitnessLineage | None = None
    reconciliation_lineage: AuthoritativeReconciliationLineage | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator(
        "provider_id", "verification_authority_id", "verification_root_id", "verification_root_version",
        "subject_id", "policy_version", mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("issued_at", "observed_at", "retrieved_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        codes = tuple(sorted(set(value)))
        if any(not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", code) is None for code in codes):
            raise ValueError("external trust reason codes must be canonical")
        return codes

    @model_validator(mode="after")
    def validate_statement(self) -> "ExternalTrustAttestationStatement":
        if not self.issued_at <= self.observed_at <= self.retrieved_at < self.expires_at:
            raise ValueError("external attestation chronology is inconsistent")
        if self.capability in {
            ExternalTrustCapability.ROOT_PROVISIONING,
            ExternalTrustCapability.LIFECYCLE_STATUS,
        }:
            if (
                self.lifecycle is None
                or self.reconciliation_status is not ExternalReconciliationStatus.NOT_APPLICABLE
                or self.evidence_fingerprint is not None
                or self.broker_lineage is not None
                or self.witness_lineage is not None
                or self.reconciliation_lineage is not None
            ):
                raise ValueError("governance attestations require lifecycle-only state")
        elif self.capability is ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION:
            if (
                self.lifecycle is not None
                or self.evidence_fingerprint is None
                or self.broker_lineage is None
                or self.witness_lineage is not None
                or self.reconciliation_lineage is not None
                or self.reconciliation_status is ExternalReconciliationStatus.NOT_APPLICABLE
            ):
                raise ValueError("broker attestations require evidence and reconciliation state")
        elif self.capability is ExternalTrustCapability.ROLLBACK_WITNESS:
            if (
                self.lifecycle is not None
                or self.evidence_fingerprint is not None
                or self.broker_lineage is not None
                or self.witness_lineage is None
                or self.reconciliation_lineage is not None
                or self.reconciliation_status is not ExternalReconciliationStatus.NOT_APPLICABLE
            ):
                raise ValueError("rollback witness attestations require checkpoint-only state")
        elif (
            self.lifecycle is not None
            or self.evidence_fingerprint is None
            or self.broker_lineage is not None
            or self.witness_lineage is not None
            or self.reconciliation_lineage is None
            or self.reconciliation_status is ExternalReconciliationStatus.NOT_APPLICABLE
        ):
            raise ValueError("reconciliation attestations require exact result lineage")
        if self.broker_lineage is not None and self.evidence_fingerprint != self.broker_lineage.evidence_fingerprint:
            raise ValueError("broker attestation evidence lineage is inconsistent")
        if self.broker_lineage is not None and (
            self.subject_fingerprint != self.broker_lineage.evidence_fingerprint
            or self.scope_fingerprint != self.broker_lineage.evidence_request_fingerprint
        ):
            raise ValueError("broker attestation subject and scope must bind the exact snapshot")
        if self.capability is not ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION and self.broker_lineage is not None:
            raise ValueError("non-broker attestations cannot contain broker lineage")
        if self.witness_lineage is not None and (
            self.subject_fingerprint != self.witness_lineage.checkpoint_fingerprint
            or self.scope_fingerprint != self.witness_lineage.replay_stream_fingerprint
            or self.source_sequence != self.witness_lineage.witness_sequence
        ):
            raise ValueError("rollback witness attestation lineage is inconsistent")
        if self.reconciliation_lineage is not None and (
            self.subject_fingerprint
            != self.reconciliation_lineage.reconciliation_fingerprint
            or self.scope_fingerprint != self.reconciliation_lineage.scope_fingerprint
            or self.evidence_fingerprint != self.reconciliation_lineage.evidence_fingerprint
            or self.source_sequence
            != self.reconciliation_lineage.reconciliation_generation
        ):
            raise ValueError("authoritative reconciliation lineage is inconsistent")
        if (
            self.reconciliation_lineage is not None
            and (
                self.reconciliation_lineage.status == "matched"
                or self.reconciliation_lineage.coverage_complete
            )
            != (self.reconciliation_status is ExternalReconciliationStatus.COMPLETE)
        ):
            raise ValueError("reconciliation status and coverage are inconsistent")
        if self.lifecycle is ExternalTrustLifecycle.ACTIVE and self.reason_codes:
            raise ValueError("active lifecycle attestations cannot contain failure reasons")
        if self.lifecycle is not None and self.lifecycle is not ExternalTrustLifecycle.ACTIVE and not self.reason_codes:
            raise ValueError("non-active lifecycle attestations require failure reasons")
        if self.reconciliation_status is ExternalReconciliationStatus.UNRESOLVED and not self.reason_codes:
            raise ValueError("unresolved reconciliation requires failure reasons")
        return self

    @computed_field(return_type=str)
    @property
    def statement_fingerprint(self) -> str:
        return sha256(canonical_external_attestation_payload(self)).hexdigest()


class SignedExternalTrustAttestation(ExternalTrustModel):
    schema_version: Literal["signed-external-trust-attestation/v1"] = (
        "signed-external-trust-attestation/v1"
    )
    statement: ExternalTrustAttestationStatement
    signature_base64: str = Field(min_length=88, max_length=88)

    @field_validator("signature_base64")
    @classmethod
    def valid_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError("external attestation signature is malformed") from error
        if len(decoded) != 64:
            raise ValueError("external attestation requires an Ed25519 signature")
        return value


class ExternalTrustAttestationAssessment(ExternalTrustModel):
    schema_version: Literal["external-trust-attestation-assessment/v1"] = (
        "external-trust-attestation-assessment/v1"
    )
    status: ExternalTrustIntegrationStatus
    capability: ExternalTrustCapability
    provider_id: str = Field(min_length=1, max_length=160)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    statement_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    source_sequence: int | None = Field(default=None, gt=0)
    validated_statement: ExternalTrustAttestationStatement | None = None
    replay_binding_fingerprints: tuple[str, ...] = ()
    replay_consumed: bool = False
    signature_verified: bool = False
    bindings_verified: bool = False
    reason_codes: tuple[str, ...] = ()
    independently_authenticated: Literal[False] = False
    transport_authenticated: Literal[False] = False
    authoritative_lifecycle: Literal[False] = False
    external_rollback_assurance: Literal[False] = False
    broker_evidence_authenticated: Literal[False] = False
    reconciliation_authoritative: Literal[False] = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def normalize_codes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def enforce_non_authority(self) -> "ExternalTrustAttestationAssessment":
        verified = self.signature_verified or self.bindings_verified
        if verified and (self.statement_fingerprint is None or self.source_sequence is None):
            raise ValueError("verified attestations require immutable statement lineage")
        if self.bindings_verified and (
            self.statement_fingerprint is None
            or self.source_sequence is None
            or self.validated_statement is None
        ):
            raise ValueError("binding-verified attestations require the validated statement")
        if self.validated_statement is not None and (
            self.validated_statement.statement_fingerprint != self.statement_fingerprint
            or self.validated_statement.source_sequence != self.source_sequence
            or self.validated_statement.request_fingerprint != self.request_fingerprint
        ):
            raise ValueError("assessment statement lineage is inconsistent")
        if self.replay_consumed:
            if (
                not self.bindings_verified
                or len(self.replay_binding_fingerprints) != 2
                or len(set(self.replay_binding_fingerprints)) != 2
                or any(
                    re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in self.replay_binding_fingerprints
                )
            ):
                raise ValueError("consumed attestations require two durable replay bindings")
        elif self.replay_binding_fingerprints:
            raise ValueError("unconsumed attestations cannot advertise replay bindings")
        if self.status is ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED:
            if not (self.signature_verified and self.bindings_verified and self.replay_consumed):
                raise ValueError("configured attestation requires signature and binding verification")
            if self.reason_codes != ("EXTERNAL_VERIFICATION_ROOT_NOT_INDEPENDENTLY_AUTHENTICATED",):
                raise ValueError("configured attestation must disclose unavailable independent trust")
        elif not self.reason_codes:
            raise ValueError("unsuccessful external trust assessments require reason codes")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def canonical_external_attestation_payload(statement: ExternalTrustAttestationStatement) -> bytes:
    return json.dumps(
        statement.model_dump(mode="json", exclude={"statement_fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external trust timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()
