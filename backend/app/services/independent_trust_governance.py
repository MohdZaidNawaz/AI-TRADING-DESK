"""Fail-closed trust-governance and external rollback-witness validation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.models.independent_trust_governance import (
    ExternalRollbackWitnessAssessment,
    ExternalRollbackWitnessObservation,
    ExternalRollbackWitnessRequest,
    ExternalRollbackWitnessStatus,
    IndependentTrustGovernanceAssessment,
    IndependentTrustGovernanceStatus,
    SignedTrustAnchorGovernanceManifest,
    TrustAnchorGovernanceRequest,
    TrustAnchorLifecycle,
    canonical_governance_manifest_payload,
)
from app.models.independent_trust_verification import ConfiguredEd25519TrustAnchor


class IndependentTrustGovernanceVerifier:
    """Verify governance manifests without claiming independent root provenance."""

    def __init__(self, governance_root: ConfiguredEd25519TrustAnchor | None = None) -> None:
        self._root = governance_root

    def assess(
        self,
        *,
        request: TrustAnchorGovernanceRequest,
        envelope: SignedTrustAnchorGovernanceManifest | None,
        evaluated_at: datetime,
        previous_envelope: SignedTrustAnchorGovernanceManifest | None = None,
    ) -> IndependentTrustGovernanceAssessment:
        now = _utc(evaluated_at)
        root = self._root
        if root is None:
            return _governance_failure(
                request=request,
                now=now,
                status=IndependentTrustGovernanceStatus.UNCONFIGURED,
                codes=("INDEPENDENT_GOVERNANCE_ROOT_UNCONFIGURED",),
            )
        if now < root.valid_from:
            return _governance_failure(
                request=request,
                now=now,
                status=IndependentTrustGovernanceStatus.GOVERNANCE_UNAVAILABLE,
                codes=("GOVERNANCE_ROOT_NOT_YET_VALID",),
                root=root,
            )
        if root.expires_at is not None and now >= root.expires_at:
            return _governance_failure(
                request=request,
                now=now,
                status=IndependentTrustGovernanceStatus.EXPIRED,
                codes=("GOVERNANCE_ROOT_EXPIRED",),
                root=root,
            )
        if envelope is None:
            return _governance_failure(
                request=request,
                now=now,
                status=IndependentTrustGovernanceStatus.GOVERNANCE_UNAVAILABLE,
                codes=("GOVERNANCE_MANIFEST_UNAVAILABLE",),
                root=root,
            )

        manifest = envelope.manifest
        codes = _governance_binding_codes(request=request, root=root, envelope=envelope, now=now)
        if not _signature_valid(root, envelope):
            codes.append("GOVERNANCE_MANIFEST_SIGNATURE_INVALID")
        codes.extend(
            _rotation_codes(
                root=root,
                current=envelope,
                previous=previous_envelope,
            )
        )
        if codes:
            status = (
                IndependentTrustGovernanceStatus.EXPIRED
                if any(code.endswith("EXPIRED") for code in codes)
                else IndependentTrustGovernanceStatus.INVALID
            )
            return _governance_failure(
                request=request,
                now=now,
                status=status,
                codes=tuple(codes),
                root=root,
                envelope=envelope,
            )

        common = dict(
            request_fingerprint=request.request_fingerprint,
            evaluated_at=now,
            governance_root_fingerprint=root.public_key_fingerprint,
            manifest_fingerprint=manifest.manifest_fingerprint,
            anchor_generation=manifest.anchor_generation,
            lifecycle=manifest.lifecycle,
            signature_verified=True,
            lineage_verified=True,
        )
        if manifest.lifecycle is TrustAnchorLifecycle.REVOKED:
            return IndependentTrustGovernanceAssessment(
                status=IndependentTrustGovernanceStatus.REVOKED,
                reason_codes=(manifest.reason_code or "ANCHOR_REVOKED",),
                **common,
            )
        if manifest.lifecycle in {
            TrustAnchorLifecycle.ROTATION_PENDING,
            TrustAnchorLifecycle.SUPERSEDED,
        }:
            return IndependentTrustGovernanceAssessment(
                status=IndependentTrustGovernanceStatus.ROTATION_PENDING,
                reason_codes=(manifest.reason_code or "ANCHOR_ROTATION_INCOMPLETE",),
                **common,
            )
        # Configuring a key and validating its signature does not prove that the
        # governance root was provisioned by an independent authority.
        return IndependentTrustGovernanceAssessment(
            status=IndependentTrustGovernanceStatus.CONFIGURED_UNAUTHENTICATED,
            reason_codes=("GOVERNANCE_ROOT_NOT_INDEPENDENTLY_AUTHENTICATED",),
            **common,
        )


@runtime_checkable
class ReadOnlyExternalRollbackWitnessProvider(Protocol):
    """Future read-only witness seam; protocol conformance is not authority."""

    @property
    def provider_id(self) -> str: ...

    def observe(self, request: ExternalRollbackWitnessRequest) -> ExternalRollbackWitnessObservation: ...


class UnavailableExternalRollbackWitnessProvider:
    provider_id = "UNCONFIGURED_EXTERNAL_ROLLBACK_WITNESS"

    def observe(self, request: ExternalRollbackWitnessRequest) -> ExternalRollbackWitnessObservation:
        return ExternalRollbackWitnessObservation(
            available=False,
            provider_id=self.provider_id,
            request_fingerprint=request.request_fingerprint,
            challenge_id=request.challenge_id,
            retrieved_at=request.requested_at,
            reason_codes=("EXTERNAL_ROLLBACK_WITNESS_UNAVAILABLE",),
        )


class ExternalRollbackWitnessIntegration:
    """Validate witness-shaped evidence while keeping external assurance false."""

    def __init__(self, provider: ReadOnlyExternalRollbackWitnessProvider | None = None) -> None:
        self._provider = provider or UnavailableExternalRollbackWitnessProvider()

    def assess(
        self, *, request: ExternalRollbackWitnessRequest, evaluated_at: datetime
    ) -> ExternalRollbackWitnessAssessment:
        now = _utc(evaluated_at)
        provider_id = _provider_id(self._provider)
        try:
            observation = self._provider.observe(request)
        except Exception:
            return _witness_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalRollbackWitnessStatus.UNAVAILABLE,
                codes=("EXTERNAL_ROLLBACK_WITNESS_PROVIDER_ERROR",),
            )
        if not isinstance(observation, ExternalRollbackWitnessObservation):
            return _witness_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalRollbackWitnessStatus.INVALID,
                codes=("EXTERNAL_ROLLBACK_WITNESS_RESULT_INVALID",),
            )

        codes: list[str] = []
        if provider_id != request.expected_provider_id or observation.provider_id != provider_id:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_PROVIDER_MISMATCH")
        if observation.request_fingerprint != request.request_fingerprint:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_REQUEST_MISMATCH")
        if observation.challenge_id != request.challenge_id:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_CHALLENGE_MISMATCH")
        if observation.retrieved_at < request.requested_at or observation.retrieved_at > now:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_RETRIEVAL_TIME_INVALID")

        if not observation.available:
            return _witness_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalRollbackWitnessStatus.UNAVAILABLE,
                codes=tuple(codes) + observation.reason_codes,
                observation=observation,
            )

        if observation.authority_id != request.authority_id:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_AUTHORITY_MISMATCH")
        if observation.anchor_generation != request.anchor_generation:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_TRUST_GENERATION_MISMATCH")
        if observation.anchor_fingerprint != request.anchor_fingerprint:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_ANCHOR_MISMATCH")
        if observation.replay_store_id != request.replay_store_id:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_STORE_MISMATCH")
        if observation.replay_checkpoint_fingerprint != request.replay_checkpoint_fingerprint:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_CHECKPOINT_MISMATCH")
        assert observation.observed_at is not None and observation.expires_at is not None
        if observation.observed_at > observation.retrieved_at or observation.expires_at <= now:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_CHRONOLOGY_INVALID")
        elif (now - observation.observed_at).total_seconds() > request.max_age_seconds:
            codes.append("EXTERNAL_ROLLBACK_WITNESS_STALE")
        if codes:
            status = (
                ExternalRollbackWitnessStatus.STALE
                if "EXTERNAL_ROLLBACK_WITNESS_STALE" in codes
                else ExternalRollbackWitnessStatus.INCONSISTENT
            )
            return _witness_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=status,
                codes=tuple(codes),
                observation=observation,
            )

        return ExternalRollbackWitnessAssessment(
            status=ExternalRollbackWitnessStatus.STRUCTURALLY_VALIDATED,
            request_fingerprint=request.request_fingerprint,
            provider_id=provider_id,
            evaluated_at=now,
            observation_fingerprint=observation.observation_fingerprint,
            reason_codes=("EXTERNAL_WITNESS_AUTHENTICATION_NOT_CONFIGURED",),
        )


def _governance_binding_codes(
    *,
    request: TrustAnchorGovernanceRequest,
    root: ConfiguredEd25519TrustAnchor,
    envelope: SignedTrustAnchorGovernanceManifest,
    now: datetime,
) -> list[str]:
    manifest = envelope.manifest
    checks = (
        (manifest.governance_authority_id == root.authority_id, "GOVERNANCE_AUTHORITY_MISMATCH"),
        (manifest.governance_root_id == root.anchor_id, "GOVERNANCE_ROOT_ID_MISMATCH"),
        (manifest.governance_root_version == root.anchor_version, "GOVERNANCE_ROOT_VERSION_MISMATCH"),
        (manifest.governance_root_fingerprint == root.public_key_fingerprint, "GOVERNANCE_ROOT_FINGERPRINT_MISMATCH"),
        (manifest.request_fingerprint == request.request_fingerprint, "GOVERNANCE_REQUEST_MISMATCH"),
        (manifest.challenge_id == request.challenge_id, "GOVERNANCE_CHALLENGE_MISMATCH"),
        (manifest.authority_id == request.authority_id, "GOVERNED_AUTHORITY_MISMATCH"),
        (manifest.anchor_id == request.anchor_id, "GOVERNED_ANCHOR_ID_MISMATCH"),
        (manifest.anchor_version == request.anchor_version, "GOVERNED_ANCHOR_VERSION_MISMATCH"),
        (manifest.anchor_generation == request.anchor_generation, "GOVERNED_ANCHOR_GENERATION_MISMATCH"),
        (manifest.anchor_fingerprint == request.anchor_fingerprint, "GOVERNED_ANCHOR_FINGERPRINT_MISMATCH"),
        (manifest.scope_fingerprint == request.scope.fingerprint, "GOVERNED_SCOPE_MISMATCH"),
        (manifest.policy_version == request.policy_version, "GOVERNED_POLICY_VERSION_MISMATCH"),
        (manifest.source_sequence >= request.minimum_source_sequence, "GOVERNANCE_SEQUENCE_ROLLBACK"),
    )
    codes = [code for valid, code in checks if not valid]
    if request.requested_at > now or manifest.issued_at < request.requested_at or manifest.issued_at > now:
        codes.append("GOVERNANCE_MANIFEST_CHRONOLOGY_INVALID")
    elif (now - manifest.issued_at).total_seconds() > request.max_age_seconds:
        codes.append("GOVERNANCE_MANIFEST_STALE")
    if now < manifest.valid_from:
        codes.append("GOVERNANCE_MANIFEST_NOT_YET_VALID")
    if now >= manifest.expires_at:
        codes.append("GOVERNANCE_MANIFEST_EXPIRED")
    return codes


def _rotation_codes(
    *,
    root: ConfiguredEd25519TrustAnchor,
    current: SignedTrustAnchorGovernanceManifest,
    previous: SignedTrustAnchorGovernanceManifest | None,
) -> tuple[str, ...]:
    manifest = current.manifest
    if manifest.anchor_generation == 1:
        return ("GOVERNANCE_UNEXPECTED_PREVIOUS_MANIFEST",) if previous is not None else ()
    if previous is None:
        return ("GOVERNANCE_ROTATION_LINEAGE_MISSING",)
    prior = previous.manifest
    codes: list[str] = []
    if not _signature_valid(root, previous):
        codes.append("PREVIOUS_GOVERNANCE_MANIFEST_SIGNATURE_INVALID")
    if manifest.previous_manifest_fingerprint != prior.manifest_fingerprint:
        codes.append("GOVERNANCE_PREVIOUS_MANIFEST_MISMATCH")
    if prior.anchor_generation + 1 != manifest.anchor_generation:
        codes.append("GOVERNANCE_ROTATION_GENERATION_INVALID")
    if prior.source_sequence >= manifest.source_sequence:
        codes.append("GOVERNANCE_ROTATION_SEQUENCE_INVALID")
    if (
        prior.governance_authority_id != manifest.governance_authority_id
        or prior.governance_root_id != manifest.governance_root_id
        or prior.governance_root_version != manifest.governance_root_version
        or prior.governance_root_fingerprint != manifest.governance_root_fingerprint
        or prior.authority_id != manifest.authority_id
        or prior.anchor_id != manifest.anchor_id
        or prior.scope_fingerprint != manifest.scope_fingerprint
        or prior.policy_version != manifest.policy_version
    ):
        codes.append("GOVERNANCE_ROTATION_LINEAGE_CONFLICT")
    if prior.lifecycle is not TrustAnchorLifecycle.ROTATION_PENDING:
        codes.append("GOVERNANCE_ROTATION_NOT_AUTHORIZED")
    if (
        prior.replacement_anchor_fingerprint != manifest.anchor_fingerprint
        or prior.replacement_generation != manifest.anchor_generation
    ):
        codes.append("GOVERNANCE_ROTATION_TARGET_MISMATCH")
    return tuple(codes)


def _signature_valid(
    root: ConfiguredEd25519TrustAnchor,
    envelope: SignedTrustAnchorGovernanceManifest,
) -> bool:
    try:
        key = serialization.load_pem_public_key(root.public_key_pem.encode("ascii"))
        assert isinstance(key, Ed25519PublicKey)
        key.verify(
            envelope.signature_bytes,
            canonical_governance_manifest_payload(envelope.manifest),
        )
        return True
    except (InvalidSignature, ValueError, TypeError, AssertionError):
        return False


def _governance_failure(
    *,
    request: TrustAnchorGovernanceRequest,
    now: datetime,
    status: IndependentTrustGovernanceStatus,
    codes: tuple[str, ...],
    root: ConfiguredEd25519TrustAnchor | None = None,
    envelope: SignedTrustAnchorGovernanceManifest | None = None,
) -> IndependentTrustGovernanceAssessment:
    return IndependentTrustGovernanceAssessment(
        status=status,
        request_fingerprint=request.request_fingerprint,
        evaluated_at=now,
        governance_root_fingerprint=None if root is None else root.public_key_fingerprint,
        manifest_fingerprint=None if envelope is None else envelope.manifest.manifest_fingerprint,
        anchor_generation=None if envelope is None else envelope.manifest.anchor_generation,
        lifecycle=None if envelope is None else envelope.manifest.lifecycle,
        reason_codes=tuple(sorted(set(codes))) or ("INDEPENDENT_GOVERNANCE_UNAVAILABLE",),
    )


def _witness_failure(
    *,
    request: ExternalRollbackWitnessRequest,
    provider_id: str,
    now: datetime,
    status: ExternalRollbackWitnessStatus,
    codes: tuple[str, ...],
    observation: ExternalRollbackWitnessObservation | None = None,
) -> ExternalRollbackWitnessAssessment:
    return ExternalRollbackWitnessAssessment(
        status=status,
        request_fingerprint=request.request_fingerprint,
        provider_id=provider_id,
        evaluated_at=now,
        observation_fingerprint=None if observation is None else observation.observation_fingerprint,
        reason_codes=tuple(sorted(set(codes))) or ("EXTERNAL_ROLLBACK_WITNESS_UNAVAILABLE",),
    )


def _provider_id(provider: object) -> str:
    try:
        value = getattr(provider, "provider_id", "")
    except Exception:
        return "INVALID_EXTERNAL_WITNESS_PROVIDER"
    normalized = str(value).strip().upper()
    return normalized or "INVALID_EXTERNAL_WITNESS_PROVIDER"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trust-governance evaluation timestamps require timezone awareness")
    return value.astimezone(UTC)
