"""Non-authorizing personal policy signing and verification boundary.

No production Windows key provider is available with the repository's current
dependency set.  The default provider therefore fails closed.  Protocol
implementations can support isolated cryptographic tests, but every result is
structurally unable to authorize production or execution.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.personal_policy_signing import (
    PersonalApprovalReplayResult,
    PersonalApprovalReplayStatus,
    PersonalKeyProtection,
    PersonalKeyProviderAvailability,
    PersonalKeyProviderStatus,
    PersonalKeyRecoveryStatus,
    PersonalKeyRevocation,
    PersonalKeyRotation,
    PersonalPolicyApprovalPayload,
    PersonalPolicySignatureEnvelope,
    PersonalPolicyVerificationResult,
    PersonalPolicyVerificationStatus,
    PersonalPublicKeyDescriptor,
    PersonalRevocationAvailability,
    PersonalTrustSnapshot,
    PersonalTrustState,
    canonical_personal_policy_payload,
)


class PersonalKeyProviderUnavailable(RuntimeError):
    """Raised when protected private-key operations cannot be established."""


@runtime_checkable
class PersonalPolicyKeyProvider(Protocol):
    """Key lifecycle interface; conformance does not establish trust or authority."""

    @property
    def provider_id(self) -> str: ...

    def status(self) -> PersonalKeyProviderStatus: ...

    def create_key(self, *, key_id: str, created_at: datetime) -> PersonalPublicKeyDescriptor: ...

    def export_public_key(self, *, key_id: str) -> PersonalPublicKeyDescriptor: ...

    def sign(self, *, key_id: str, payload: bytes) -> bytes: ...

    def verify(
        self, *, descriptor: PersonalPublicKeyDescriptor, payload: bytes, signature: bytes
    ) -> bool: ...

    def rotate_key(
        self, *, previous_key_id: str, replacement_key_id: str, rotated_at: datetime
    ) -> tuple[PersonalPublicKeyDescriptor, PersonalKeyRotation]: ...

    def revoke_key(self, *, key_id: str, revoked_at: datetime, reason: str) -> PersonalKeyRevocation: ...

    def recovery_status(self, *, key_id: str) -> PersonalKeyRecoveryStatus: ...


@runtime_checkable
class PersonalApprovalReplayProtector(Protocol):
    """Replay boundary; an implementation alone never grants authority."""

    def check_and_record(
        self, *, approval_id: str, payload_fingerprint: str, checked_at: datetime
    ) -> PersonalApprovalReplayResult: ...


class UnavailableWindowsProtectedKeyProvider:
    """Fail-closed default: no plaintext or unprotected-key fallback."""

    provider_id = "WINDOWS_PROTECTED_KEY_PROVIDER_UNAVAILABLE"

    def status(self) -> PersonalKeyProviderStatus:
        return PersonalKeyProviderStatus(
            provider_id=self.provider_id,
            availability=PersonalKeyProviderAvailability.UNAVAILABLE,
            protection=PersonalKeyProtection.UNAVAILABLE,
            platform="WINDOWS",
            reason_codes=("WINDOWS_PROTECTED_KEY_API_UNAVAILABLE",),
        )

    def create_key(self, *, key_id: str, created_at: datetime) -> PersonalPublicKeyDescriptor:
        raise PersonalKeyProviderUnavailable("Windows protected key creation is unavailable")

    def export_public_key(self, *, key_id: str) -> PersonalPublicKeyDescriptor:
        raise PersonalKeyProviderUnavailable("Windows protected public-key export is unavailable")

    def sign(self, *, key_id: str, payload: bytes) -> bytes:
        raise PersonalKeyProviderUnavailable("Windows protected policy signing is unavailable")

    def verify(
        self, *, descriptor: PersonalPublicKeyDescriptor, payload: bytes, signature: bytes
    ) -> bool:
        raise PersonalKeyProviderUnavailable("Windows protected signature verification is unavailable")

    def rotate_key(
        self, *, previous_key_id: str, replacement_key_id: str, rotated_at: datetime
    ) -> tuple[PersonalPublicKeyDescriptor, PersonalKeyRotation]:
        raise PersonalKeyProviderUnavailable("Windows protected key rotation is unavailable")

    def revoke_key(self, *, key_id: str, revoked_at: datetime, reason: str) -> PersonalKeyRevocation:
        raise PersonalKeyProviderUnavailable("Windows protected key revocation is unavailable")

    def recovery_status(self, *, key_id: str) -> PersonalKeyRecoveryStatus:
        return PersonalKeyRecoveryStatus(
            key_id=key_id,
            metadata_available=False,
            signing_capability_available=False,
            requires_external_recovery=True,
            reason_codes=("WINDOWS_PROTECTED_KEY_RECOVERY_UNAVAILABLE",),
        )


class UnavailablePersonalApprovalReplayProtector:
    def check_and_record(
        self, *, approval_id: str, payload_fingerprint: str, checked_at: datetime
    ) -> PersonalApprovalReplayResult:
        return PersonalApprovalReplayResult(
            status=PersonalApprovalReplayStatus.UNAVAILABLE,
            approval_id=approval_id,
            payload_fingerprint=payload_fingerprint,
            checked_at=checked_at,
            reason_codes=("PERSONAL_APPROVAL_REPLAY_PROTECTION_UNAVAILABLE",),
        )


class PersonalPolicySigningService:
    """Create a detached signature without assigning authority to it."""

    def __init__(self, provider: PersonalPolicyKeyProvider | None = None) -> None:
        self._provider = provider or UnavailableWindowsProtectedKeyProvider()

    def sign(self, payload: PersonalPolicyApprovalPayload) -> PersonalPolicySignatureEnvelope:
        status = self._provider.status()
        if (
            not isinstance(status, PersonalKeyProviderStatus)
            or status.availability is not PersonalKeyProviderAvailability.AVAILABLE
            or status.protection is PersonalKeyProtection.UNAVAILABLE
        ):
            raise PersonalKeyProviderUnavailable("protected policy signing is unavailable")
        descriptor = self._provider.export_public_key(key_id=payload.trust_key_id)
        if not isinstance(descriptor, PersonalPublicKeyDescriptor):
            raise PersonalKeyProviderUnavailable("key provider returned invalid public metadata")
        if descriptor.key_id != payload.trust_key_id or descriptor.protection is not status.protection:
            raise PersonalKeyProviderUnavailable("key provider metadata does not match the signing request")
        if descriptor.expires_at is not None and payload.issued_at >= descriptor.expires_at:
            raise PersonalKeyProviderUnavailable("signing key is expired")
        signature = self._provider.sign(
            key_id=payload.trust_key_id,
            payload=canonical_personal_policy_payload(payload),
        )
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise PersonalKeyProviderUnavailable("key provider returned an invalid Ed25519 signature")
        return PersonalPolicySignatureEnvelope(
            approval_id=payload.approval_id,
            key_id=payload.trust_key_id,
            payload_fingerprint=payload.payload_fingerprint,
            signature_base64=base64.b64encode(signature).decode("ascii"),
        )


class PersonalPolicyVerificationService:
    """Validate integrity and replay state while remaining non-authorizing."""

    def __init__(
        self,
        provider: PersonalPolicyKeyProvider | None = None,
        replay_protector: PersonalApprovalReplayProtector | None = None,
    ) -> None:
        self._provider = provider or UnavailableWindowsProtectedKeyProvider()
        self._replay = replay_protector or UnavailablePersonalApprovalReplayProtector()

    def verify(
        self,
        *,
        payload: PersonalPolicyApprovalPayload,
        envelope: PersonalPolicySignatureEnvelope,
        expected_policy: ControlledDemoExecutionPolicy,
        trust: PersonalTrustSnapshot,
        verified_at: datetime,
    ) -> PersonalPolicyVerificationResult:
        now = _utc(verified_at)
        codes: list[str] = []
        if not _exact_policy(payload, expected_policy):
            codes.append("PERSONAL_POLICY_BINDING_MISMATCH")
        if envelope.approval_id != payload.approval_id:
            codes.append("PERSONAL_APPROVAL_ID_MISMATCH")
        if envelope.key_id != payload.trust_key_id:
            codes.append("PERSONAL_APPROVAL_KEY_MISMATCH")
        if envelope.payload_fingerprint != payload.payload_fingerprint:
            codes.append("PERSONAL_APPROVAL_PAYLOAD_FINGERPRINT_MISMATCH")
        if payload.issued_at > now:
            codes.append("PERSONAL_APPROVAL_ISSUED_IN_FUTURE")
        if now >= payload.expires_at:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.EXPIRED,
                codes=tuple(codes) + ("PERSONAL_APPROVAL_EXPIRED",),
            )
        codes.extend(_trust_codes(trust=trust, key_id=envelope.key_id, now=now))
        revoked = envelope.key_id in {item.key_id for item in trust.revocations}
        if revoked:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.REVOKED,
                codes=tuple(codes) + ("PERSONAL_TRUST_KEY_REVOKED",),
            )
        descriptor = next((item for item in trust.active_keys if item.key_id == envelope.key_id), None)
        if descriptor is None:
            codes.append("PERSONAL_TRUST_KEY_UNKNOWN_OR_ROTATED")
        if codes or descriptor is None:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.REJECTED,
                codes=tuple(codes),
            )

        try:
            provider_status = self._provider.status()
            exported = self._provider.export_public_key(key_id=descriptor.key_id)
            verified = (
                isinstance(provider_status, PersonalKeyProviderStatus)
                and provider_status.availability is PersonalKeyProviderAvailability.AVAILABLE
                and isinstance(exported, PersonalPublicKeyDescriptor)
                and exported.public_key_fingerprint == descriptor.public_key_fingerprint
                and exported.generation == descriptor.generation
                and self._provider.verify(
                    descriptor=descriptor,
                    payload=canonical_personal_policy_payload(payload),
                    signature=envelope.signature_bytes,
                )
            )
        except Exception:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.UNAVAILABLE,
                codes=("PERSONAL_SIGNATURE_VERIFIER_UNAVAILABLE",),
            )
        if not verified:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.REJECTED,
                codes=("PERSONAL_SIGNATURE_INVALID",),
            )

        if (
            trust.state is not PersonalTrustState.TEST_ONLY
            or provider_status.protection is not PersonalKeyProtection.TEST_ONLY
            or descriptor.protection is not PersonalKeyProtection.TEST_ONLY
        ):
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.UNAVAILABLE,
                codes=("INDEPENDENT_PERSONAL_TRUST_NOT_ESTABLISHED",),
                cryptographic_integrity=True,
            )

        try:
            replay = self._replay.check_and_record(
                approval_id=payload.approval_id,
                payload_fingerprint=payload.payload_fingerprint,
                checked_at=now,
            )
        except Exception:
            replay = None
        if not isinstance(replay, PersonalApprovalReplayResult):
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.UNAVAILABLE,
                codes=("PERSONAL_APPROVAL_REPLAY_PROTECTION_UNAVAILABLE",),
                cryptographic_integrity=True,
            )
        if replay.status in {PersonalApprovalReplayStatus.DUPLICATE, PersonalApprovalReplayStatus.CONFLICT}:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.REPLAYED,
                codes=replay.reason_codes,
                cryptographic_integrity=True,
            )
        if replay.status is not PersonalApprovalReplayStatus.ACCEPTED:
            return _result(
                payload=payload,
                envelope=envelope,
                now=now,
                status=PersonalPolicyVerificationStatus.UNAVAILABLE,
                codes=replay.reason_codes or ("PERSONAL_APPROVAL_REPLAY_PROTECTION_UNAVAILABLE",),
                cryptographic_integrity=True,
            )
        return _result(
            payload=payload,
            envelope=envelope,
            now=now,
            status=PersonalPolicyVerificationStatus.TEST_ONLY_VERIFIED,
            codes=(),
            cryptographic_integrity=True,
        )


def _exact_policy(payload: PersonalPolicyApprovalPayload, policy: ControlledDemoExecutionPolicy) -> bool:
    return (
        payload.policy_id == policy.policy_id
        and payload.policy_version == policy.policy_version
        and payload.policy_fingerprint == policy.fingerprint
        and payload.policy.fingerprint == policy.fingerprint
    )


def _trust_codes(*, trust: PersonalTrustSnapshot, key_id: str, now: datetime) -> tuple[str, ...]:
    codes: list[str] = []
    if trust.state is PersonalTrustState.UNCONFIGURED:
        codes.append("PERSONAL_TRUST_UNCONFIGURED")
    if trust.revocation_availability is not PersonalRevocationAvailability.AVAILABLE:
        codes.append("PERSONAL_REVOCATION_STATE_UNAVAILABLE")
    if trust.retrieved_at > now or trust.observed_at > trust.retrieved_at:
        codes.append("PERSONAL_TRUST_TIMESTAMP_INVALID")
    elif (now - trust.observed_at).total_seconds() > trust.max_age_seconds:
        codes.append("PERSONAL_TRUST_STATE_STALE")
    descriptor = next((item for item in trust.active_keys if item.key_id == key_id), None)
    if descriptor is not None and descriptor.expires_at is not None and now >= descriptor.expires_at:
        codes.append("PERSONAL_TRUST_KEY_EXPIRED")
    return tuple(codes)


def _result(
    *,
    payload: PersonalPolicyApprovalPayload,
    envelope: PersonalPolicySignatureEnvelope,
    now: datetime,
    status: PersonalPolicyVerificationStatus,
    codes: tuple[str, ...],
    cryptographic_integrity: bool = False,
) -> PersonalPolicyVerificationResult:
    return PersonalPolicyVerificationResult(
        status=status,
        approval_id=payload.approval_id,
        payload_fingerprint=payload.payload_fingerprint,
        key_id=envelope.key_id,
        verified_at=now,
        cryptographic_integrity_verified=cryptographic_integrity,
        reason_codes=tuple(sorted(set(codes))),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("personal policy verification time requires timezone awareness")
    return value.astimezone(UTC)
