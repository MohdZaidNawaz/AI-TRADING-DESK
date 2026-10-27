"""Immutable Phase 21 contracts for external-dependency acceptance evidence.

These models are descriptive and diagnostic.  They cannot grant production
authority, submission permission, or execution authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.schemas import APIModel


_FINGERPRINT = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"
_IDENTIFIER = r"^[a-z0-9][a-z0-9._:/-]{0,127}$"
_REASON = r"^[A-Z][A-Z0-9_]{0,119}$"
_SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|password|secret|token|credential|authorization|private[_-]?key)\s*[:=]"
)


class DependencyAcceptanceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalDependencyId(str, Enum):
    INDEPENDENT_TRUST_ROOT = "independent_trust_root"
    LIFECYCLE_REVOCATION_AUTHORITY = "lifecycle_revocation_authority"
    MONOTONIC_ROLLBACK_WITNESS = "monotonic_rollback_witness"
    READ_ONLY_BROKER_EVIDENCE = "read_only_broker_evidence"
    AUTHORITATIVE_RECONCILIATION = "authoritative_reconciliation"
    MONITORING_ALERT_DELIVERY = "monitoring_alert_delivery"
    BACKUP_RECOVERY_REHEARSAL = "backup_recovery_rehearsal"
    INDEPENDENT_SECURITY_REVIEW = "independent_security_review"
    HUMAN_PRODUCTION_APPROVAL = "human_production_approval"


class DependencyMaturity(str, Enum):
    DESIGNED = "designed"
    LOCAL_CONTRACT = "local_contract"
    SYNTHETIC_TESTED = "synthetic_tested"
    SANDBOX_CONNECTED = "sandbox_connected"
    INDEPENDENTLY_VERIFIED = "independently_verified"
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    INVALID = "invalid"
    CONFLICTING = "conflicting"
    REVOKED = "revoked"


class IntegrationEnvironment(str, Enum):
    LOCAL_DEVELOPMENT = "local_development"
    SYNTHETIC_TEST = "synthetic_test"
    ISOLATED_SANDBOX = "isolated_sandbox"
    VERIFIED_PREPRODUCTION = "verified_preproduction"
    PRODUCTION = "production"


class DependencyEvidenceType(str, Enum):
    TRUST_ROOT_PROVISIONING = "trust_root_provisioning"
    LIFECYCLE_STATUS = "lifecycle_status"
    ROLLBACK_WITNESS_CONFIRMATION = "rollback_witness_confirmation"
    BROKER_SNAPSHOT = "broker_snapshot"
    RECONCILIATION_DECISION = "reconciliation_decision"
    ALERT_DELIVERY_RECEIPT = "alert_delivery_receipt"
    RECOVERY_REHEARSAL_RESULT = "recovery_rehearsal_result"
    SECURITY_REVIEW_DECISION = "security_review_decision"
    HUMAN_APPROVAL_DECISION = "human_approval_decision"


class EvidenceVerificationResult(str, Enum):
    NOT_VERIFIED = "not_verified"
    SYNTHETIC_ONLY = "synthetic_only"
    SANDBOX_ONLY = "sandbox_only"
    INDEPENDENTLY_VERIFIED = "independently_verified"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class EvidenceProvenance(str, Enum):
    LOCAL_CONTRACT = "local_contract"
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    SANDBOX_PROVIDER = "sandbox_provider"
    INDEPENDENT_AUTHORITY = "independent_authority"
    MANUAL_CLAIM = "manual_claim"


class EvidenceRevocationState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    COMPROMISED = "compromised"


class VerificationMode(str, Enum):
    AUTOMATED_EXTERNAL = "automated_external"
    MANUAL_INDEPENDENT = "manual_independent"
    HYBRID = "hybrid"


class BrokerEvidenceField(str, Enum):
    ACCOUNT_STATE = "account_state"
    INSTRUMENT_METADATA = "instrument_metadata"
    POSITIONS = "positions"
    EXPOSURE = "exposure"
    MARKET_REFERENCE = "market_reference"


class DeliveryOutcome(str, Enum):
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RehearsalOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class ConsistencyOutcome(str, Enum):
    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    UNKNOWN = "unknown"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REMEDIATION_REQUIRED = "remediation_required"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"


class DependencyEvidencePayload(DependencyAcceptanceModel):
    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class TrustDependencyPayload(DependencyEvidencePayload):
    payload_type: Literal[
        "trust_root_provisioning",
        "lifecycle_status",
        "rollback_witness_confirmation",
    ]
    lineage_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    authority_identity_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )


class BrokerEvidencePayload(DependencyEvidencePayload):
    payload_type: Literal["broker_snapshot"] = "broker_snapshot"
    source_identity_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    account_scope_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    snapshot_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    snapshot_generation: int = Field(ge=1)
    required_fields: tuple[BrokerEvidenceField, ...]
    lineage_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    read_only_capability_claimed: bool
    capability_evidence_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    unresolved_conflicts: int = Field(ge=0)

    @field_validator("required_fields")
    @classmethod
    def stable_fields(cls, value: tuple[BrokerEvidenceField, ...]) -> tuple[BrokerEvidenceField, ...]:
        if len(value) != len(set(value)):
            raise ValueError("broker evidence fields must be unique")
        return tuple(sorted(value, key=lambda item: item.value))


class ReconciliationEvidencePayload(DependencyEvidencePayload):
    payload_type: Literal["reconciliation_decision"] = "reconciliation_decision"
    source_identity_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    snapshot_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    snapshot_generation: int = Field(ge=1)
    expected_record_count: int = Field(ge=0)
    covered_record_count: int = Field(ge=0)
    complete: bool
    unresolved_discrepancies: int = Field(ge=0)
    conflicting_generation: bool


class MonitoringDeliveryPayload(DependencyEvidencePayload):
    payload_type: Literal["alert_delivery_receipt"] = "alert_delivery_receipt"
    signal_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    test_event_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    outcome: DeliveryOutcome
    receipt_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_IDENTIFIER)
    receipt_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    event_at: datetime
    destination_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)

    @field_validator("event_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def receipt_coherence(self) -> "MonitoringDeliveryPayload":
        if self.outcome is DeliveryOutcome.DELIVERED and (
            self.receipt_id is None or self.receipt_fingerprint is None
        ):
            raise ValueError("delivered monitoring evidence requires a receipt")
        return self


class RecoveryRehearsalPayload(DependencyEvidencePayload):
    payload_type: Literal["recovery_rehearsal_result"] = "recovery_rehearsal_result"
    backup_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    rehearsal_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    started_at: datetime
    completed_at: datetime | None
    observed_rpo_seconds: int | None = Field(default=None, ge=0)
    observed_rto_seconds: int | None = Field(default=None, ge=0)
    maximum_rpo_seconds: int = Field(ge=0)
    maximum_rto_seconds: int = Field(ge=0)
    replay_consistency: ConsistencyOutcome
    witness_consistency: ConsistencyOutcome
    outcome: RehearsalOutcome
    evidence_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def chronology(self) -> "RecoveryRehearsalPayload":
        if self.completed_at is not None and self.completed_at <= self.started_at:
            raise ValueError("recovery completion must follow start")
        return self


class SecurityReviewPayload(DependencyEvidencePayload):
    payload_type: Literal["security_review_decision"] = "security_review_decision"
    reviewer_identity_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    reviewer_organization_role: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER
    )
    independence_attested: bool
    review_scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    target_commit: str = Field(min_length=40, max_length=40, pattern=_COMMIT)
    finding_count: int = Field(ge=0)
    unresolved_critical_count: int = Field(ge=0)
    unresolved_high_count: int = Field(ge=0)
    outstanding_blocker_count: int = Field(ge=0)
    decision: ReviewDecision
    decision_expires_at: datetime
    evidence_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)

    @field_validator("decision_expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)


class HumanApprovalPayload(DependencyEvidencePayload):
    payload_type: Literal["human_approval_decision"] = "human_approval_decision"
    approver_identity_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    approver_role: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    approved_scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    target_commit: str = Field(min_length=40, max_length=40, pattern=_COMMIT)
    policy_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    issued_at: datetime
    expires_at: datetime
    revocation_state: EvidenceRevocationState
    decision_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    decision: ApprovalDecision
    evidence_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def chronology(self) -> "HumanApprovalPayload":
        if self.expires_at <= self.issued_at:
            raise ValueError("human approval expiry must follow issuance")
        return self


TypedDependencyPayload = Annotated[
    TrustDependencyPayload
    | BrokerEvidencePayload
    | ReconciliationEvidencePayload
    | MonitoringDeliveryPayload
    | RecoveryRehearsalPayload
    | SecurityReviewPayload
    | HumanApprovalPayload,
    Field(discriminator="payload_type"),
]


class DependencyAcceptanceRegistryEntry(DependencyAcceptanceModel):
    schema_version: Literal["dependency-acceptance-registry-entry/v1"] = (
        "dependency-acceptance-registry-entry/v1"
    )
    dependency_id: ExternalDependencyId
    purpose: str = Field(min_length=1, max_length=400)
    security_boundary: str = Field(min_length=1, max_length=400)
    required_evidence_types: tuple[DependencyEvidenceType, ...] = Field(min_length=1)
    owner_role: str = Field(min_length=1, max_length=120)
    independent_verification_method: str = Field(min_length=1, max_length=400)
    freshness_seconds: int = Field(ge=1, le=31_536_000)
    identity_scope_requirements: tuple[str, ...] = Field(min_length=1)
    expected_failure_modes: tuple[str, ...] = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    verification_mode: VerificationMode
    current_maturity: Literal[
        DependencyMaturity.LOCAL_CONTRACT,
        DependencyMaturity.SYNTHETIC_TESTED,
    ] = DependencyMaturity.LOCAL_CONTRACT
    blocker_codes: tuple[str, ...] = Field(min_length=1)
    allowed_environments: tuple[IntegrationEnvironment, ...] = Field(min_length=1)
    retention_and_redaction: str = Field(min_length=1, max_length=400)
    requires_signature: bool
    requires_monotonic_sequence: bool
    requires_commit_binding: bool

    @field_validator(
        "purpose",
        "security_boundary",
        "owner_role",
        "independent_verification_method",
        "retention_and_redaction",
    )
    @classmethod
    def safe_scalar_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("registry descriptions require printable non-empty text")
        if _SENSITIVE.search(normalized):
            raise ValueError("registry descriptions cannot contain secret material")
        return normalized

    @field_validator(
        "identity_scope_requirements",
        "expected_failure_modes",
        "acceptance_criteria",
        mode="before",
    )
    @classmethod
    def stable_text(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        normalized = tuple(sorted({" ".join(str(item).split()) for item in value}))
        if any(not item or len(item) > 240 or _SENSITIVE.search(item) for item in normalized):
            raise ValueError("registry text must be bounded, non-secret, and non-empty")
        return normalized

    @field_validator("blocker_codes", mode="before")
    @classmethod
    def stable_codes(cls, value: object) -> object:
        return _reason_codes(value)

    @field_validator("required_evidence_types", "allowed_environments")
    @classmethod
    def unique_enums(cls, value: tuple[Enum, ...]) -> tuple[Enum, ...]:
        if len(value) != len(set(value)):
            raise ValueError("registry lists must not contain duplicates")
        return tuple(sorted(value, key=lambda item: item.value))

    @model_validator(mode="after")
    def unavailable_contract_has_explicit_boundaries(self) -> "DependencyAcceptanceRegistryEntry":
        if "REAL_INDEPENDENT_DEPENDENCY_UNAVAILABLE" not in self.blocker_codes:
            raise ValueError("local dependency contracts require an explicit external blocker")
        required_local = {
            IntegrationEnvironment.LOCAL_DEVELOPMENT,
            IntegrationEnvironment.SYNTHETIC_TEST,
        }
        if not required_local.issubset(set(self.allowed_environments)):
            raise ValueError("dependency contracts must permit local and synthetic validation")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class DependencyAcceptanceRegistry(DependencyAcceptanceModel):
    schema_version: Literal["dependency-acceptance-registry/v1"] = (
        "dependency-acceptance-registry/v1"
    )
    policy_version: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    entries: tuple[DependencyAcceptanceRegistryEntry, ...]
    production_acceptance_available: Literal[False] = False
    authorizes_execution: Literal[False] = False

    @model_validator(mode="after")
    def complete_registry(self) -> "DependencyAcceptanceRegistry":
        expected = set(ExternalDependencyId)
        actual = [entry.dependency_id for entry in self.entries]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("dependency registry requires every dependency exactly once")
        if tuple(actual) != tuple(sorted(actual, key=lambda item: item.value)):
            raise ValueError("dependency registry ordering must be deterministic")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class ExternalDependencyEvidenceEnvelope(DependencyAcceptanceModel):
    """Sanitized evidence metadata; never raw payload or authority by itself."""

    schema_version: Literal["external-dependency-evidence/v1"] = (
        "external-dependency-evidence/v1"
    )
    evidence_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    dependency_id: ExternalDependencyId
    evidence_type: DependencyEvidenceType
    issuer_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    subject_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    environment: IntegrationEnvironment
    request_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    challenge_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    payload_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    payload: TypedDependencyPayload
    signature_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    trust_chain_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=_FINGERPRINT
    )
    issued_at: datetime
    expires_at: datetime
    verified_at: datetime | None = None
    source_sequence: int | None = Field(default=None, ge=1)
    previous_source_sequence: int | None = Field(default=None, ge=0)
    provenance: EvidenceProvenance
    verification_result: EvidenceVerificationResult
    revocation_state: EvidenceRevocationState
    synthetic: bool
    acceptance_policy_version: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER
    )
    commit_hash: str | None = Field(default=None, min_length=40, max_length=40, pattern=_COMMIT)
    reason_codes: tuple[str, ...] = ()

    @field_validator("issued_at", "expires_at", "verified_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dependency evidence timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def stable_reasons(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def coherent_evidence(self) -> "ExternalDependencyEvidenceEnvelope":
        if self.payload.payload_type != self.evidence_type.value:
            raise ValueError("dependency payload type must match evidence type")
        if self.payload.fingerprint != self.payload_fingerprint:
            raise ValueError("dependency payload fingerprint must match canonical payload")
        if self.expires_at <= self.issued_at:
            raise ValueError("dependency evidence expiry must follow issuance")
        if self.verified_at is not None and self.verified_at < self.issued_at:
            raise ValueError("dependency evidence cannot be verified before issuance")
        if (self.source_sequence is None) != (self.previous_source_sequence is None):
            raise ValueError("sequence lineage requires both current and previous values")
        if (
            self.source_sequence is not None
            and self.previous_source_sequence is not None
            and self.source_sequence != self.previous_source_sequence + 1
        ):
            raise ValueError("dependency evidence sequence must advance exactly once")
        if self.synthetic:
            if self.provenance is not EvidenceProvenance.SYNTHETIC_FIXTURE:
                raise ValueError("synthetic evidence requires synthetic provenance")
            if self.verification_result is not EvidenceVerificationResult.SYNTHETIC_ONLY:
                raise ValueError("synthetic evidence cannot claim external verification")
        if self.verification_result is EvidenceVerificationResult.INDEPENDENTLY_VERIFIED:
            if self.synthetic or self.provenance is not EvidenceProvenance.INDEPENDENT_AUTHORITY:
                raise ValueError("independent verification requires real independent provenance")
            if self.signature_fingerprint is None or self.trust_chain_fingerprint is None:
                raise ValueError("independent verification requires signed trust-chain metadata")
            if self.verified_at is None:
                raise ValueError("independent verification requires a verification timestamp")
        if self.verification_result in {
            EvidenceVerificationResult.INVALID,
            EvidenceVerificationResult.UNAVAILABLE,
        } and not self.reason_codes:
            raise ValueError("failed dependency evidence requires reason codes")
        return self

    @computed_field(return_type=str)
    @property
    def envelope_fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"envelope_fingerprint"}))


class DependencyAcceptanceContext(DependencyAcceptanceModel):
    schema_version: Literal["dependency-acceptance-context/v1"] = (
        "dependency-acceptance-context/v1"
    )
    environment: IntegrationEnvironment
    subject_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    request_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    challenge_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    scope_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    acceptance_policy_version: str = Field(
        min_length=1, max_length=128, pattern=_IDENTIFIER
    )
    commit_hash: str = Field(min_length=40, max_length=40, pattern=_COMMIT)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dependency acceptance requires timezone-aware evaluation")
        return value.astimezone(UTC)


class DependencyAcceptanceAssessment(DependencyAcceptanceModel):
    dependency_id: ExternalDependencyId
    maturity: DependencyMaturity
    reason_codes: tuple[str, ...]
    evidence_fingerprints: tuple[str, ...] = ()
    evaluated_at: datetime
    independently_verified: Literal[False] = False
    operationally_accepted: Literal[False] = False
    authorizes_execution: Literal[False] = False

    @field_validator("reason_codes", mode="before")
    @classmethod
    def stable_reasons(cls, value: object) -> object:
        return _reason_codes(value)

    @field_validator("evidence_fingerprints")
    @classmethod
    def stable_fingerprints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(re.fullmatch(_FINGERPRINT, item) is None for item in normalized):
            raise ValueError("assessment evidence fingerprints must be SHA-256 values")
        return normalized

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dependency assessment requires timezone-aware evaluation")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def remains_non_authorizing(self) -> "DependencyAcceptanceAssessment":
        if self.maturity in {
            DependencyMaturity.INDEPENDENTLY_VERIFIED,
            DependencyMaturity.ACCEPTED,
        }:
            raise ValueError("offline Phase 21 assessments cannot establish acceptance")
        if not self.reason_codes:
            raise ValueError("offline dependency assessments require blocker reasons")
        return self


class DependencyAcceptanceReport(DependencyAcceptanceModel):
    schema_version: Literal["dependency-acceptance-report/v1"] = (
        "dependency-acceptance-report/v1"
    )
    assessment_kind: Literal["offline_external_dependency_acceptance"] = (
        "offline_external_dependency_acceptance"
    )
    status: Literal["blocked"] = "blocked"
    context: DependencyAcceptanceContext
    registry_fingerprint: str = Field(min_length=64, max_length=64, pattern=_FINGERPRINT)
    assessments: tuple[DependencyAcceptanceAssessment, ...]
    blocker_codes: tuple[str, ...]
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("blocker_codes", mode="before")
    @classmethod
    def stable_reasons(cls, value: object) -> object:
        return _reason_codes(value)

    @model_validator(mode="after")
    def complete_report(self) -> "DependencyAcceptanceReport":
        expected = set(ExternalDependencyId)
        actual = [assessment.dependency_id for assessment in self.assessments]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("acceptance reports require every dependency exactly once")
        if tuple(actual) != tuple(sorted(actual, key=lambda item: item.value)):
            raise ValueError("acceptance report ordering must be deterministic")
        if any(item.evaluated_at != self.context.evaluated_at for item in self.assessments):
            raise ValueError("acceptance report requires one evaluation timestamp")
        derived = tuple(
            sorted({code for item in self.assessments for code in item.reason_codes})
        )
        if self.blocker_codes != derived:
            raise ValueError("report blockers must exactly match dependency blockers")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def _reason_codes(value: object) -> object:
    if not isinstance(value, (tuple, list)):
        return value
    normalized = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
    if any(re.fullmatch(_REASON, item) is None for item in normalized):
        raise ValueError("dependency acceptance reason codes must be canonical")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dependency evidence timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
