"""No-I/O-by-default integration boundary for external trust attestations."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.models.authenticated_external_trust import (
    BrokerEvidenceLineage,
    ExternalReconciliationStatus,
    ExternalTrustAttestationAssessment,
    ExternalTrustAttestationRequest,
    ExternalTrustCapability,
    ExternalTrustIntegrationStatus,
    ExternalTrustLifecycle,
    SignedExternalTrustAttestation,
    canonical_external_attestation_payload,
)
from app.models.execution_state import ReconciliationReport, ReconciliationStatus
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor
from app.models.independent_trust_verification import IndependentReplayBinding
from app.models.trusted_execution_evidence import ValidatedExecutionEvidence
from app.services.durable_independent_replay import DurableIndependentReplayStore


_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 2.0
_MAX_PROVIDER_TIMEOUT_SECONDS = 30.0
_PROVIDER_CALL_SLOTS = BoundedSemaphore(4)


@runtime_checkable
class ReadOnlyExternalTrustAttestationProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def read_attestation(
        self, request: ExternalTrustAttestationRequest
    ) -> SignedExternalTrustAttestation | None: ...


class UnavailableExternalTrustAttestationProvider:
    provider_id = "UNCONFIGURED_EXTERNAL_TRUST_PROVIDER"

    def read_attestation(
        self, request: ExternalTrustAttestationRequest
    ) -> SignedExternalTrustAttestation | None:
        del request
        return None


class AuthenticatedExternalTrustIntegration:
    """Validate signed facts while refusing to manufacture independent trust."""

    def __init__(
        self,
        provider: ReadOnlyExternalTrustAttestationProvider | None = None,
        *,
        verification_root: ConfiguredEd25519TrustAnchor | None = None,
        replay_store: DurableIndependentReplayStore | None = None,
        provider_timeout_seconds: float = _DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider or UnavailableExternalTrustAttestationProvider()
        self._root = verification_root
        self._replay_store = (
            replay_store if type(replay_store) is DurableIndependentReplayStore else None
        )
        timeout = float(provider_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > _MAX_PROVIDER_TIMEOUT_SECONDS:
            raise ValueError("external trust provider timeout is outside the safe bound")
        self._provider_timeout_seconds = timeout

    def assess(
        self, request: ExternalTrustAttestationRequest, *, evaluated_at: datetime
    ) -> ExternalTrustAttestationAssessment:
        now = _utc(evaluated_at)
        root = self._root
        provider_id = _provider_id(self._provider)
        if root is None:
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.UNCONFIGURED,
                            "EXTERNAL_VERIFICATION_ROOT_UNCONFIGURED")
        if now < root.valid_from or (root.expires_at is not None and now >= root.expires_at):
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.STALE,
                            "EXTERNAL_VERIFICATION_ROOT_NOT_CURRENT")
        if provider_id != request.expected_provider_id:
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.INVALID,
                            "EXTERNAL_PROVIDER_IDENTITY_MISMATCH")
        call_status, envelope = _bounded_provider_read(
            self._provider, request, timeout_seconds=self._provider_timeout_seconds
        )
        if call_status != "completed":
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.UNAVAILABLE,
                            call_status)
        if envelope is None:
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.UNAVAILABLE,
                            "EXTERNAL_TRUST_ATTESTATION_UNAVAILABLE")
        if not isinstance(envelope, SignedExternalTrustAttestation):
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.INVALID,
                            "EXTERNAL_TRUST_PROVIDER_MALFORMED")

        statement = envelope.statement
        codes = _binding_codes(request, root, provider_id, envelope, now)
        signature_verified = _signature_valid(root, envelope)
        if not signature_verified:
            codes.append("EXTERNAL_ATTESTATION_SIGNATURE_INVALID")
        if codes:
            status = (
                ExternalTrustIntegrationStatus.STALE
                if any("STALE" in code or "EXPIRED" in code for code in codes)
                else ExternalTrustIntegrationStatus.INVALID
            )
            return _failure(
                request, provider_id, now, status, *codes,
                envelope=envelope, signature_verified=signature_verified,
            )
        if self._replay_store is None:
            return _failure(
                request,
                provider_id,
                now,
                ExternalTrustIntegrationStatus.UNAVAILABLE,
                "DURABLE_EXTERNAL_ATTESTATION_REPLAY_UNAVAILABLE",
                envelope=envelope,
                signature_verified=True,
                bindings_verified=True,
            )
        replay = self._replay_store.check_and_record_atomic(
            bindings=_replay_bindings(request, root, envelope),
            checked_at=now,
        )
        if replay.status != "accepted":
            status = (
                ExternalTrustIntegrationStatus.UNAVAILABLE
                if replay.status == "unavailable"
                else ExternalTrustIntegrationStatus.INVALID
            )
            return _failure(
                request,
                provider_id,
                now,
                status,
                *replay.reason_codes,
                envelope=envelope,
                signature_verified=True,
                bindings_verified=True,
            )
        if statement.lifecycle in {
            ExternalTrustLifecycle.REVOKED,
            ExternalTrustLifecycle.COMPROMISED,
            ExternalTrustLifecycle.RECOVERY_REQUIRED,
        }:
            return _failure(
                request, provider_id, now, ExternalTrustIntegrationStatus.REVOKED,
                *(statement.reason_codes or ("EXTERNAL_SUBJECT_NOT_ACTIVE",)),
                envelope=envelope, signature_verified=True, bindings_verified=True,
                replay_binding_fingerprints=replay.binding_fingerprints,
            )
        if statement.lifecycle is ExternalTrustLifecycle.SUPERSEDED:
            return _failure(
                request, provider_id, now, ExternalTrustIntegrationStatus.SUPERSEDED,
                *(statement.reason_codes or ("EXTERNAL_SUBJECT_SUPERSEDED",)),
                envelope=envelope, signature_verified=True, bindings_verified=True,
                replay_binding_fingerprints=replay.binding_fingerprints,
            )
        if statement.reconciliation_status is ExternalReconciliationStatus.UNRESOLVED:
            return _failure(
                request, provider_id, now, ExternalTrustIntegrationStatus.CONFLICT,
                *(statement.reason_codes or ("EXTERNAL_RECONCILIATION_UNRESOLVED",)),
                envelope=envelope, signature_verified=True, bindings_verified=True,
                replay_binding_fingerprints=replay.binding_fingerprints,
            )
        if (
            statement.witness_lineage is not None
            and statement.witness_lineage.update_outcome != "confirmed"
        ):
            return _failure(
                request,
                provider_id,
                now,
                ExternalTrustIntegrationStatus.CONFLICT,
                "EXTERNAL_WITNESS_UPDATE_UNCERTAIN",
                envelope=envelope,
                signature_verified=True,
                bindings_verified=True,
                replay_binding_fingerprints=replay.binding_fingerprints,
            )
        if (
            statement.reconciliation_lineage is not None
            and statement.reconciliation_lineage.status != "matched"
        ):
            return _failure(
                request,
                provider_id,
                now,
                ExternalTrustIntegrationStatus.CONFLICT,
                "EXTERNAL_RECONCILIATION_INCOMPLETE",
                envelope=envelope,
                signature_verified=True,
                bindings_verified=True,
                replay_binding_fingerprints=replay.binding_fingerprints,
            )
        # The configured root model is deliberately Literal[False] for
        # independently_trusted.  A valid local configuration therefore proves
        # structure only and cannot authorize production.
        return ExternalTrustAttestationAssessment(
            status=ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED,
            capability=request.capability,
            provider_id=provider_id,
            request_fingerprint=request.request_fingerprint,
            evaluated_at=now,
            statement_fingerprint=statement.statement_fingerprint,
            source_sequence=statement.source_sequence,
            validated_statement=statement,
            replay_binding_fingerprints=replay.binding_fingerprints,
            replay_consumed=True,
            signature_verified=True,
            bindings_verified=True,
            reason_codes=("EXTERNAL_VERIFICATION_ROOT_NOT_INDEPENDENTLY_AUTHENTICATED",),
        )


def build_broker_evidence_lineage(
    evidence: ValidatedExecutionEvidence,
    reconciliation: ReconciliationReport,
) -> BrokerEvidenceLineage:
    """Build the exact typed context an external broker statement must sign."""

    if (
        not evidence.ready
        or evidence.source_observed_at is None
        or evidence.source_retrieved_at is None
        or reconciliation.status is not ReconciliationStatus.MATCHED
    ):
        raise ValueError("broker attestation requires complete matched evidence lineage")
    return BrokerEvidenceLineage(
        evidence_provider_id=evidence.provider_id,
        evidence_request_fingerprint=evidence.request.fingerprint,
        evidence_fingerprint=evidence.fingerprint,
        account_fingerprint=_digest(evidence.request.account.model_dump(mode="json")),
        snapshot_observed_at=evidence.source_observed_at,
        snapshot_retrieved_at=evidence.source_retrieved_at,
        reconciliation_id=reconciliation.execution_id,
        reconciliation_fingerprint=_digest(reconciliation.model_dump(mode="json")),
    )


def _binding_codes(
    request: ExternalTrustAttestationRequest,
    root: ConfiguredEd25519TrustAnchor,
    provider_id: str,
    envelope: SignedExternalTrustAttestation,
    now: datetime,
) -> list[str]:
    item = envelope.statement
    checks = (
        (item.provider_id == provider_id, "EXTERNAL_ATTESTATION_PROVIDER_MISMATCH"),
        (item.verification_authority_id == root.authority_id, "EXTERNAL_VERIFICATION_AUTHORITY_MISMATCH"),
        (item.verification_root_id == root.anchor_id, "EXTERNAL_VERIFICATION_ROOT_ID_MISMATCH"),
        (item.verification_root_version == root.anchor_version, "EXTERNAL_VERIFICATION_ROOT_VERSION_MISMATCH"),
        (item.verification_root_fingerprint == root.public_key_fingerprint, "EXTERNAL_VERIFICATION_ROOT_FINGERPRINT_MISMATCH"),
        (item.request_fingerprint == request.request_fingerprint, "EXTERNAL_ATTESTATION_REQUEST_MISMATCH"),
        (item.challenge_id == request.challenge_id, "EXTERNAL_ATTESTATION_CHALLENGE_MISMATCH"),
        (item.capability is request.capability, "EXTERNAL_ATTESTATION_CAPABILITY_MISMATCH"),
        (item.subject_id == request.subject_id, "EXTERNAL_ATTESTATION_SUBJECT_MISMATCH"),
        (item.subject_generation == request.subject_generation, "EXTERNAL_ATTESTATION_GENERATION_MISMATCH"),
        (item.subject_fingerprint == request.subject_fingerprint, "EXTERNAL_ATTESTATION_FINGERPRINT_MISMATCH"),
        (item.scope_fingerprint == request.scope_fingerprint, "EXTERNAL_ATTESTATION_SCOPE_MISMATCH"),
        (item.policy_version == request.policy_version, "EXTERNAL_ATTESTATION_POLICY_MISMATCH"),
        (item.broker_lineage == request.broker_lineage, "EXTERNAL_ATTESTATION_BROKER_LINEAGE_MISMATCH"),
        (item.witness_lineage == request.witness_lineage, "EXTERNAL_ATTESTATION_WITNESS_LINEAGE_MISMATCH"),
        (
            item.reconciliation_lineage == request.reconciliation_lineage,
            "EXTERNAL_ATTESTATION_RECONCILIATION_LINEAGE_MISMATCH",
        ),
        (item.source_sequence >= request.minimum_source_sequence, "EXTERNAL_ATTESTATION_SEQUENCE_ROLLBACK"),
    )
    codes = [code for valid, code in checks if not valid]
    if request.requested_at > now or item.issued_at < request.requested_at or item.issued_at > now:
        codes.append("EXTERNAL_ATTESTATION_CHRONOLOGY_INVALID")
    if item.retrieved_at < request.requested_at or item.retrieved_at > now:
        codes.append("EXTERNAL_ATTESTATION_RETRIEVAL_CHRONOLOGY_INVALID")
    if item.expires_at <= now:
        codes.append("EXTERNAL_ATTESTATION_EXPIRED")
    if now - item.observed_at > timedelta(seconds=request.max_age_seconds):
        codes.append("EXTERNAL_ATTESTATION_STALE")
    return sorted(set(codes))


def _signature_valid(
    root: ConfiguredEd25519TrustAnchor, envelope: SignedExternalTrustAttestation
) -> bool:
    try:
        key = serialization.load_pem_public_key(root.public_key_pem.encode("ascii"))
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(
            base64.b64decode(envelope.signature_base64, validate=True),
            canonical_external_attestation_payload(envelope.statement),
        )
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def _failure(
    request: ExternalTrustAttestationRequest,
    provider_id: str,
    now: datetime,
    status: ExternalTrustIntegrationStatus,
    *codes: str,
    envelope: SignedExternalTrustAttestation | None = None,
    signature_verified: bool = False,
    bindings_verified: bool = False,
    replay_binding_fingerprints: tuple[str, ...] = (),
) -> ExternalTrustAttestationAssessment:
    statement = None if envelope is None else envelope.statement
    preserve_statement = statement if signature_verified and bindings_verified else None
    return ExternalTrustAttestationAssessment(
        status=status,
        capability=request.capability,
        provider_id=provider_id,
        request_fingerprint=request.request_fingerprint,
        evaluated_at=now,
        statement_fingerprint=None if statement is None else statement.statement_fingerprint,
        source_sequence=None if statement is None else statement.source_sequence,
        validated_statement=preserve_statement,
        replay_binding_fingerprints=replay_binding_fingerprints,
        replay_consumed=bool(replay_binding_fingerprints),
        signature_verified=signature_verified,
        bindings_verified=bindings_verified,
        reason_codes=tuple(sorted(set(codes))),
    )


def _provider_id(provider: object) -> str:
    value = getattr(provider, "provider_id", "INVALID_EXTERNAL_TRUST_PROVIDER")
    if not isinstance(value, str) or not value.strip():
        return "INVALID_EXTERNAL_TRUST_PROVIDER"
    return value.strip().upper()


def _bounded_provider_read(
    provider: ReadOnlyExternalTrustAttestationProvider,
    request: ExternalTrustAttestationRequest,
    *,
    timeout_seconds: float,
) -> tuple[str, SignedExternalTrustAttestation | None]:
    """Run one provider call with a process-wide bound on abandoned work.

    Python cannot safely terminate a blocked thread.  At most four daemon calls
    may remain outstanding; after that the boundary immediately fails closed.
    Late results are discarded and are never cached as authority.
    """

    if not _PROVIDER_CALL_SLOTS.acquire(blocking=False):
        return "EXTERNAL_TRUST_PROVIDER_CAPACITY_UNAVAILABLE", None
    outcome: Queue[tuple[str, SignedExternalTrustAttestation | None]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            value = provider.read_attestation(request)
            outcome.put_nowait(("completed", value))
        except Exception:
            outcome.put_nowait(("EXTERNAL_TRUST_PROVIDER_UNAVAILABLE", None))
        finally:
            _PROVIDER_CALL_SLOTS.release()

    worker = Thread(target=invoke, name="friday-external-trust-read", daemon=True)
    try:
        worker.start()
    except Exception:
        _PROVIDER_CALL_SLOTS.release()
        return "EXTERNAL_TRUST_PROVIDER_UNAVAILABLE", None
    try:
        return outcome.get(timeout=timeout_seconds)
    except Empty:
        return "EXTERNAL_TRUST_PROVIDER_TIMEOUT", None


def _replay_bindings(
    request: ExternalTrustAttestationRequest,
    root: ConfiguredEd25519TrustAnchor,
    envelope: SignedExternalTrustAttestation,
) -> tuple[IndependentReplayBinding, IndependentReplayBinding]:
    statement = envelope.statement
    challenge_context = {
        "provider_id": statement.provider_id,
        "verification_authority_id": statement.verification_authority_id,
        "verification_root_id": statement.verification_root_id,
        "challenge_id": statement.challenge_id,
    }
    challenge_digest = _digest(challenge_context)
    challenge_binding = IndependentReplayBinding(
        authority_id=root.authority_id,
        anchor_id=root.anchor_id,
        anchor_version=root.anchor_version,
        anchor_fingerprint=root.public_key_fingerprint,
        key_id=f"EXTERNAL-CHALLENGE-{challenge_digest}",
        key_generation=1,
        scope_fingerprint=challenge_digest,
        source_sequence=1,
        statement_id=f"challenge-{request.request_fingerprint}",
        statement_fingerprint=statement.statement_fingerprint,
    )
    stream_context = {
        "provider_id": statement.provider_id,
        "capability": statement.capability.value,
        "subject_id": statement.subject_id,
        "scope_fingerprint": statement.scope_fingerprint,
        "policy_version": statement.policy_version,
    }
    sequence_binding = IndependentReplayBinding(
        authority_id=root.authority_id,
        anchor_id=root.anchor_id,
        anchor_version=root.anchor_version,
        anchor_fingerprint=root.public_key_fingerprint,
        key_id=f"EXTERNAL-SEQUENCE-{_digest(stream_context)}",
        key_generation=statement.subject_generation,
        scope_fingerprint=statement.scope_fingerprint,
        source_sequence=statement.source_sequence,
        statement_id=statement.statement_id,
        statement_fingerprint=statement.statement_fingerprint,
    )
    return challenge_binding, sequence_binding


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external trust evaluation timestamp requires timezone awareness")
    return value.astimezone(UTC)
