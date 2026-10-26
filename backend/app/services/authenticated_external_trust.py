"""No-I/O-by-default integration boundary for external trust attestations."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.models.authenticated_external_trust import (
    ExternalReconciliationStatus,
    ExternalTrustAttestationAssessment,
    ExternalTrustAttestationRequest,
    ExternalTrustCapability,
    ExternalTrustIntegrationStatus,
    ExternalTrustLifecycle,
    SignedExternalTrustAttestation,
    canonical_external_attestation_payload,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor


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
    ) -> None:
        self._provider = provider or UnavailableExternalTrustAttestationProvider()
        self._root = verification_root

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
        try:
            envelope = self._provider.read_attestation(request)
        except Exception:
            return _failure(request, provider_id, now, ExternalTrustIntegrationStatus.UNAVAILABLE,
                            "EXTERNAL_TRUST_PROVIDER_UNAVAILABLE")
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
        if statement.lifecycle in {
            ExternalTrustLifecycle.REVOKED,
            ExternalTrustLifecycle.COMPROMISED,
            ExternalTrustLifecycle.RECOVERY_REQUIRED,
        }:
            return _failure(
                request, provider_id, now, ExternalTrustIntegrationStatus.REVOKED,
                *(statement.reason_codes or ("EXTERNAL_SUBJECT_NOT_ACTIVE",)),
                envelope=envelope, signature_verified=True, bindings_verified=True,
            )
        if statement.lifecycle is ExternalTrustLifecycle.SUPERSEDED:
            return _failure(
                request, provider_id, now, ExternalTrustIntegrationStatus.SUPERSEDED,
                *(statement.reason_codes or ("EXTERNAL_SUBJECT_SUPERSEDED",)),
                envelope=envelope, signature_verified=True, bindings_verified=True,
            )
        if statement.reconciliation_status is ExternalReconciliationStatus.UNRESOLVED:
            return _failure(
                request, provider_id, now, ExternalTrustIntegrationStatus.CONFLICT,
                *(statement.reason_codes or ("EXTERNAL_RECONCILIATION_UNRESOLVED",)),
                envelope=envelope, signature_verified=True, bindings_verified=True,
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
            signature_verified=True,
            bindings_verified=True,
            reason_codes=("EXTERNAL_VERIFICATION_ROOT_NOT_INDEPENDENTLY_AUTHENTICATED",),
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
) -> ExternalTrustAttestationAssessment:
    statement = None if envelope is None else envelope.statement
    return ExternalTrustAttestationAssessment(
        status=status,
        capability=request.capability,
        provider_id=provider_id,
        request_fingerprint=request.request_fingerprint,
        evaluated_at=now,
        statement_fingerprint=None if statement is None else statement.statement_fingerprint,
        source_sequence=None if statement is None else statement.source_sequence,
        signature_verified=signature_verified,
        bindings_verified=bindings_verified,
        reason_codes=tuple(sorted(set(codes))),
    )


def _provider_id(provider: object) -> str:
    value = getattr(provider, "provider_id", "INVALID_EXTERNAL_TRUST_PROVIDER")
    if not isinstance(value, str) or not value.strip():
        return "INVALID_EXTERNAL_TRUST_PROVIDER"
    return value.strip().upper()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external trust evaluation timestamp requires timezone awareness")
    return value.astimezone(UTC)
