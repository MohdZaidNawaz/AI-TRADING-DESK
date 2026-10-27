"""Strictly offline Phase 22 verification of signed Phase 21 evidence."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from hmac import compare_digest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.models.dependency_acceptance import (
    DependencyAcceptanceContext,
    EvidenceProvenance,
    EvidenceRevocationState,
    EvidenceVerificationResult,
    ExternalDependencyEvidenceEnvelope,
    IntegrationEnvironment,
)
from app.models.independent_evidence_verification import (
    BindingVerificationStatus,
    ConfiguredKeyLifecycle,
    EvidenceSignatureAlgorithm,
    EvidenceTrustConfiguration,
    EvidenceVerificationDisposition,
    EvidenceVerificationPolicy,
    EvidenceVerificationReason,
    EvidenceVerificationReport,
    EvidenceVerificationRequest,
    FreshnessVerificationStatus,
    IssuerAuthenticationStatus,
    LifecycleVerificationStatus,
    ReplayEvaluationStatus,
    SignatureVerificationStatus,
    SignedDependencyEvidence,
    StructuralVerificationStatus,
    canonical_dependency_evidence_payload,
)
from app.services.dependency_acceptance import build_dependency_acceptance_registry


def build_evidence_verification_policy() -> EvidenceVerificationPolicy:
    return EvidenceVerificationPolicy()


class OfflineIndependentEvidenceVerifier:
    """Verify configured-key signatures without claiming independent authority."""

    def __init__(self, configuration: EvidenceTrustConfiguration | None = None) -> None:
        if type(configuration) is EvidenceTrustConfiguration:
            try:
                self._configuration = EvidenceTrustConfiguration.model_validate(
                    configuration.model_dump(
                        exclude={"fingerprint"},
                        exclude_computed_fields=True,
                    )
                )
            except Exception:
                self._configuration = None
        else:
            self._configuration = None
        self._policy = build_evidence_verification_policy()

    def verify(
        self,
        *,
        request: EvidenceVerificationRequest,
        evaluated_at: datetime,
    ) -> EvidenceVerificationReport:
        now = _utc(evaluated_at)
        if type(request) is not EvidenceVerificationRequest:
            return self._report(
                request_fingerprint="0" * 64,
                now=now,
                disposition=EvidenceVerificationDisposition.STRUCTURALLY_INVALID,
                structural=StructuralVerificationStatus.INVALID,
                reasons=(EvidenceVerificationReason.EVIDENCE_STRUCTURE_INVALID,),
            )
        try:
            canonical_request = EvidenceVerificationRequest.model_validate(
                request.model_dump(
                    exclude={"fingerprint"},
                    exclude_computed_fields=True,
                )
            )
        except Exception:
            return self._report(
                request_fingerprint=_safe_request_fingerprint(request),
                now=now,
                disposition=EvidenceVerificationDisposition.STRUCTURALLY_INVALID,
                structural=StructuralVerificationStatus.INVALID,
                reasons=(EvidenceVerificationReason.EVIDENCE_STRUCTURE_INVALID,),
            )

        if canonical_request.policy_version != self._policy.policy_version:
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.STRUCTURALLY_INVALID,
                structural=StructuralVerificationStatus.INVALID,
                reasons=(EvidenceVerificationReason.VERIFICATION_POLICY_MISMATCH,),
            )
        if canonical_request.signed_evidence is None:
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.UNAVAILABLE,
                structural=StructuralVerificationStatus.UNAVAILABLE,
                reasons=(EvidenceVerificationReason.EVIDENCE_UNAVAILABLE,),
            )

        signed = canonical_request.signed_evidence
        evidence = signed.evidence
        evidence_fingerprint = evidence.envelope_fingerprint
        signed_fingerprint = signed.signed_payload_fingerprint
        context = canonical_request.context
        registry = build_dependency_acceptance_registry()
        entry = next(item for item in registry.entries if item.dependency_id is evidence.dependency_id)

        binding_reasons = _binding_reasons(
            evidence,
            context,
            entry.requires_commit_binding,
            evaluated_at=now,
        )
        freshness, freshness_reasons = _freshness(
            evidence=evidence,
            now=now,
            max_age_seconds=entry.freshness_seconds,
        )
        lifecycle, lifecycle_reasons = _evidence_lifecycle(evidence)
        structural = (
            StructuralVerificationStatus.INVALID
            if binding_reasons
            else StructuralVerificationStatus.VALID
        )
        if binding_reasons:
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.BINDING_INVALID,
                structural=structural,
                binding=BindingVerificationStatus.INVALID,
                freshness=freshness,
                lifecycle=lifecycle,
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                reasons=tuple(binding_reasons + freshness_reasons + lifecycle_reasons),
            )
        if freshness is not FreshnessVerificationStatus.FRESH:
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.STALE,
                structural=structural,
                binding=BindingVerificationStatus.VALID,
                freshness=freshness,
                lifecycle=lifecycle,
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                reasons=tuple(freshness_reasons + lifecycle_reasons),
            )
        if lifecycle in {
            LifecycleVerificationStatus.REVOKED,
            LifecycleVerificationStatus.SUPERSEDED,
            LifecycleVerificationStatus.COMPROMISED,
        }:
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.REVOKED,
                structural=structural,
                binding=BindingVerificationStatus.VALID,
                freshness=freshness,
                lifecycle=lifecycle,
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                reasons=tuple(lifecycle_reasons),
            )
        if lifecycle is LifecycleVerificationStatus.UNKNOWN:
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.UNAVAILABLE,
                structural=structural,
                binding=BindingVerificationStatus.VALID,
                freshness=freshness,
                lifecycle=lifecycle,
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                reasons=tuple(lifecycle_reasons),
            )

        configuration = self._configuration
        if configuration is None:
            reasons = [EvidenceVerificationReason.VERIFIER_CONFIGURATION_UNAVAILABLE]
            if signed.presented_public_key_pem is not None:
                reasons.append(EvidenceVerificationReason.EMBEDDED_PUBLIC_KEY_NOT_TRUSTED)
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.UNAVAILABLE,
                structural=structural,
                binding=BindingVerificationStatus.VALID,
                freshness=freshness,
                lifecycle=lifecycle,
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                reasons=tuple(reasons),
            )

        configured_root_fingerprint = configuration.trust_anchor.public_key_fingerprint
        configuration_reasons = _configuration_reasons(
            configuration=configuration,
            signed=signed,
            context=context,
            now=now,
        )
        identity_reasons = {
            EvidenceVerificationReason.ISSUER_IDENTITY_MISMATCH,
            EvidenceVerificationReason.KEY_IDENTITY_MISMATCH,
            EvidenceVerificationReason.KEY_GENERATION_MISMATCH,
            EvidenceVerificationReason.ROOT_VERSION_MISMATCH,
            EvidenceVerificationReason.ROOT_FINGERPRINT_MISMATCH,
            EvidenceVerificationReason.DEPENDENCY_NOT_PERMITTED,
            EvidenceVerificationReason.ENVIRONMENT_NOT_PERMITTED,
            EvidenceVerificationReason.SCOPE_NOT_PERMITTED,
        }
        issuer_status = (
            IssuerAuthenticationStatus.MISMATCH
            if any(reason in identity_reasons for reason in configuration_reasons)
            else IssuerAuthenticationStatus.UNAVAILABLE
        )
        configuration_binding_status = (
            BindingVerificationStatus.INVALID
            if any(reason in identity_reasons for reason in configuration_reasons)
            else BindingVerificationStatus.VALID
        )
        if configuration_reasons:
            if configuration.lifecycle in {
                ConfiguredKeyLifecycle.REVOKED,
                ConfiguredKeyLifecycle.SUPERSEDED,
                ConfiguredKeyLifecycle.COMPROMISED,
            }:
                disposition = EvidenceVerificationDisposition.REVOKED
            elif configuration.lifecycle is ConfiguredKeyLifecycle.UNKNOWN:
                disposition = EvidenceVerificationDisposition.UNAVAILABLE
            elif EvidenceVerificationReason.TRUST_ROOT_NOT_CURRENT in configuration_reasons:
                disposition = EvidenceVerificationDisposition.STALE
            else:
                disposition = EvidenceVerificationDisposition.BINDING_INVALID
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=disposition,
                structural=structural,
                issuer=issuer_status,
                binding=configuration_binding_status,
                freshness=freshness,
                lifecycle=_configured_lifecycle(configuration.lifecycle),
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                configured_root_fingerprint=configured_root_fingerprint,
                reasons=tuple(configuration_reasons),
            )

        issuer_status = IssuerAuthenticationStatus.CONFIGURED_SYNTHETIC_KEY

        if not _signature_valid(configuration, signed):
            return self._report(
                request_fingerprint=canonical_request.fingerprint,
                now=now,
                disposition=EvidenceVerificationDisposition.SIGNATURE_INVALID,
                structural=structural,
                signature=SignatureVerificationStatus.INVALID,
                issuer=IssuerAuthenticationStatus.UNAVAILABLE,
                binding=BindingVerificationStatus.VALID,
                freshness=freshness,
                lifecycle=lifecycle,
                evidence_fingerprint=evidence_fingerprint,
                signed_payload_fingerprint=signed_fingerprint,
                configured_root_fingerprint=configured_root_fingerprint,
                reasons=(EvidenceVerificationReason.SIGNATURE_INVALID,),
            )

        replay = (
            ReplayEvaluationStatus.DUPLICATE_IN_SUPPLIED_SET
            if evidence_fingerprint in canonical_request.previously_seen_evidence_fingerprints
            else ReplayEvaluationStatus.NOT_SEEN_IN_SUPPLIED_SET
        )
        reasons = [
            EvidenceVerificationReason.SYNTHETIC_TRUST_ROOT_NON_AUTHORITATIVE,
            EvidenceVerificationReason.INDEPENDENT_LIFECYCLE_VERIFICATION_UNAVAILABLE,
            EvidenceVerificationReason.EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE,
        ]
        if signed.presented_public_key_pem is not None:
            reasons.append(EvidenceVerificationReason.EMBEDDED_PUBLIC_KEY_NOT_TRUSTED)
        if evidence.verification_result is EvidenceVerificationResult.INDEPENDENTLY_VERIFIED:
            reasons.append(EvidenceVerificationReason.CALLER_VERIFICATION_CLAIM_IGNORED)
        if replay is ReplayEvaluationStatus.DUPLICATE_IN_SUPPLIED_SET:
            reasons.append(EvidenceVerificationReason.EVIDENCE_DUPLICATE_IN_SUPPLIED_SET)
            disposition = EvidenceVerificationDisposition.DUPLICATE
        else:
            disposition = (
                EvidenceVerificationDisposition.CONFIGURED_KEY_VERIFIED_NON_AUTHORITATIVE
            )
        return self._report(
            request_fingerprint=canonical_request.fingerprint,
            now=now,
            disposition=disposition,
            structural=structural,
            signature=SignatureVerificationStatus.VALID,
            issuer=issuer_status,
            binding=BindingVerificationStatus.VALID,
            freshness=freshness,
            lifecycle=LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED,
            replay=replay,
            evidence_fingerprint=evidence_fingerprint,
            signed_payload_fingerprint=signed_fingerprint,
            configured_root_fingerprint=configured_root_fingerprint,
            reasons=tuple(reasons),
        )

    def _report(
        self,
        *,
        request_fingerprint: str,
        now: datetime,
        disposition: EvidenceVerificationDisposition,
        structural: StructuralVerificationStatus,
        reasons: tuple[EvidenceVerificationReason, ...],
        signature: SignatureVerificationStatus = SignatureVerificationStatus.NOT_CHECKED,
        issuer: IssuerAuthenticationStatus = IssuerAuthenticationStatus.UNAVAILABLE,
        binding: BindingVerificationStatus = BindingVerificationStatus.NOT_CHECKED,
        lifecycle: LifecycleVerificationStatus = LifecycleVerificationStatus.UNAVAILABLE,
        freshness: FreshnessVerificationStatus = FreshnessVerificationStatus.NOT_CHECKED,
        replay: ReplayEvaluationStatus = ReplayEvaluationStatus.NOT_CHECKED,
        evidence_fingerprint: str | None = None,
        signed_payload_fingerprint: str | None = None,
        configured_root_fingerprint: str | None = None,
    ) -> EvidenceVerificationReport:
        return EvidenceVerificationReport(
            disposition=disposition,
            request_fingerprint=request_fingerprint,
            policy_fingerprint=self._policy.fingerprint,
            evaluated_at=now,
            evidence_fingerprint=evidence_fingerprint,
            signed_payload_fingerprint=signed_payload_fingerprint,
            configured_root_fingerprint=configured_root_fingerprint,
            structural_status=structural,
            signature_status=signature,
            issuer_status=issuer,
            binding_status=binding,
            lifecycle_status=lifecycle,
            freshness_status=freshness,
            replay_status=replay,
            reason_codes=tuple(sorted(set(reasons), key=lambda item: item.value)),
        )


def _binding_reasons(
    evidence,
    context,
    commit_required: bool,
    *,
    evaluated_at: datetime,
) -> list[EvidenceVerificationReason]:
    registry = build_dependency_acceptance_registry()
    entry = next(item for item in registry.entries if item.dependency_id is evidence.dependency_id)
    reasons: list[EvidenceVerificationReason] = []
    checks = (
        (evidence.evidence_type in entry.required_evidence_types, EvidenceVerificationReason.EVIDENCE_TYPE_MISMATCH),
        (_same(evidence.subject_id, context.subject_id), EvidenceVerificationReason.SUBJECT_BINDING_MISMATCH),
        (_same(evidence.request_id, context.request_id), EvidenceVerificationReason.REQUEST_BINDING_MISMATCH),
        (_same(evidence.challenge_id, context.challenge_id), EvidenceVerificationReason.CHALLENGE_BINDING_MISMATCH),
        (evidence.environment is context.environment, EvidenceVerificationReason.ENVIRONMENT_BINDING_MISMATCH),
        (_same(evidence.scope_fingerprint, context.scope_fingerprint), EvidenceVerificationReason.SCOPE_BINDING_MISMATCH),
        (_same(evidence.acceptance_policy_version, context.acceptance_policy_version), EvidenceVerificationReason.POLICY_BINDING_MISMATCH),
        (context.evaluated_at == evaluated_at, EvidenceVerificationReason.EVALUATION_TIME_MISMATCH),
    )
    reasons.extend(reason for valid, reason in checks if not valid)
    if commit_required and evidence.commit_hash is None:
        reasons.append(EvidenceVerificationReason.COMMIT_BINDING_MISSING)
    elif evidence.commit_hash is not None and not _same(evidence.commit_hash, context.commit_hash):
        reasons.append(EvidenceVerificationReason.COMMIT_BINDING_MISMATCH)
    return reasons


def _freshness(*, evidence, now: datetime, max_age_seconds: int):
    if evidence.issued_at > now or (evidence.verified_at is not None and evidence.verified_at > now):
        return FreshnessVerificationStatus.FUTURE_DATED, [EvidenceVerificationReason.EVIDENCE_FUTURE_DATED]
    if evidence.expires_at <= now:
        return FreshnessVerificationStatus.EXPIRED, [EvidenceVerificationReason.EVIDENCE_EXPIRED]
    if now - evidence.issued_at > timedelta(seconds=max_age_seconds):
        return FreshnessVerificationStatus.STALE, [EvidenceVerificationReason.EVIDENCE_STALE]
    return FreshnessVerificationStatus.FRESH, []


def _evidence_lifecycle(evidence):
    state = evidence.revocation_state
    if state is EvidenceRevocationState.REVOKED:
        return LifecycleVerificationStatus.REVOKED, [EvidenceVerificationReason.EVIDENCE_REVOKED]
    if state is EvidenceRevocationState.SUPERSEDED:
        return LifecycleVerificationStatus.SUPERSEDED, [EvidenceVerificationReason.EVIDENCE_SUPERSEDED]
    if state is EvidenceRevocationState.COMPROMISED:
        return LifecycleVerificationStatus.COMPROMISED, [EvidenceVerificationReason.EVIDENCE_COMPROMISED]
    if state is EvidenceRevocationState.UNKNOWN:
        return LifecycleVerificationStatus.UNKNOWN, [EvidenceVerificationReason.EVIDENCE_LIFECYCLE_UNKNOWN]
    return LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED, []


def _configuration_reasons(*, configuration, signed, context, now):
    reasons: list[EvidenceVerificationReason] = []
    root = configuration.trust_anchor
    evidence = signed.evidence
    checks = (
        (_same(evidence.issuer_id.upper(), configuration.issuer_id), EvidenceVerificationReason.ISSUER_IDENTITY_MISMATCH),
        (_same(signed.key_id, configuration.key_id), EvidenceVerificationReason.KEY_IDENTITY_MISMATCH),
        (signed.key_generation == configuration.key_generation, EvidenceVerificationReason.KEY_GENERATION_MISMATCH),
        (_same(signed.verification_root_version, root.anchor_version), EvidenceVerificationReason.ROOT_VERSION_MISMATCH),
        (_same(signed.verification_root_fingerprint, root.public_key_fingerprint), EvidenceVerificationReason.ROOT_FINGERPRINT_MISMATCH),
        (evidence.dependency_id in configuration.permitted_dependencies, EvidenceVerificationReason.DEPENDENCY_NOT_PERMITTED),
        (context.environment in configuration.permitted_environments, EvidenceVerificationReason.ENVIRONMENT_NOT_PERMITTED),
        (any(_same(context.scope_fingerprint, item) for item in configuration.permitted_scope_fingerprints), EvidenceVerificationReason.SCOPE_NOT_PERMITTED),
    )
    reasons.extend(reason for valid, reason in checks if not valid)
    if now < root.valid_from or (root.expires_at is not None and now >= root.expires_at):
        reasons.append(EvidenceVerificationReason.TRUST_ROOT_NOT_CURRENT)
    lifecycle_reason = {
        ConfiguredKeyLifecycle.REVOKED: EvidenceVerificationReason.TRUST_ROOT_REVOKED,
        ConfiguredKeyLifecycle.SUPERSEDED: EvidenceVerificationReason.TRUST_ROOT_SUPERSEDED,
        ConfiguredKeyLifecycle.COMPROMISED: EvidenceVerificationReason.TRUST_ROOT_COMPROMISED,
        ConfiguredKeyLifecycle.UNKNOWN: EvidenceVerificationReason.TRUST_ROOT_LIFECYCLE_UNKNOWN,
    }.get(configuration.lifecycle)
    if lifecycle_reason is not None:
        reasons.append(lifecycle_reason)
    return reasons


def _configured_lifecycle(value: ConfiguredKeyLifecycle) -> LifecycleVerificationStatus:
    return {
        ConfiguredKeyLifecycle.ACTIVE: LifecycleVerificationStatus.CLAIMED_ACTIVE_UNVERIFIED,
        ConfiguredKeyLifecycle.REVOKED: LifecycleVerificationStatus.REVOKED,
        ConfiguredKeyLifecycle.SUPERSEDED: LifecycleVerificationStatus.SUPERSEDED,
        ConfiguredKeyLifecycle.COMPROMISED: LifecycleVerificationStatus.COMPROMISED,
        ConfiguredKeyLifecycle.UNKNOWN: LifecycleVerificationStatus.UNKNOWN,
    }[value]


def _signature_valid(
    configuration: EvidenceTrustConfiguration,
    signed: SignedDependencyEvidence,
) -> bool:
    try:
        key = serialization.load_pem_public_key(
            configuration.trust_anchor.public_key_pem.encode("ascii")
        )
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(
            base64.b64decode(signed.signature_base64, validate=True),
            canonical_dependency_evidence_payload(signed),
        )
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def _same(left: str, right: str) -> bool:
    return compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _safe_request_fingerprint(value: object) -> str:
    try:
        candidate = getattr(value, "fingerprint", None)
    except Exception:
        return "0" * 64
    if isinstance(candidate, str) and len(candidate) == 64:
        return candidate
    return "0" * 64


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence verification requires timezone-aware evaluation")
    return value.astimezone(UTC)
