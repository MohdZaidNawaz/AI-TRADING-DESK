"""Read-only Ed25519 verification for independently configured public keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.models.independent_trust_verification import (
    ConfiguredEd25519TrustAnchor,
    IndependentKeyStatusStatement,
    IndependentReplayBinding,
    IndependentReplayResult,
    IndependentReplayStatus,
    IndependentTrustVerificationResult,
    IndependentTrustVerificationStatus,
    SignedIndependentKeyStatusStatement,
    canonical_independent_key_status_payload,
    replay_binding_for,
)
from app.models.personal_trust_bootstrap import (
    PersonalKeyAuthorityRequest,
    PersonalKeyAuthorityState,
)


@runtime_checkable
class IndependentAuthorityReplayProtector(Protocol):
    """External replay seam; protocol conformance does not establish durability."""

    def check_and_record(
        self, *, binding: IndependentReplayBinding, checked_at: datetime
    ) -> IndependentReplayResult: ...


class UnavailableIndependentAuthorityReplayProtector:
    def check_and_record(
        self, *, binding: IndependentReplayBinding, checked_at: datetime
    ) -> IndependentReplayResult:
        return IndependentReplayResult(
            status=IndependentReplayStatus.UNAVAILABLE,
            binding_fingerprint=binding.binding_fingerprint,
            checked_at=checked_at,
            reason_codes=("EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE",),
        )


class IndependentKeyStatusVerifier:
    """Verify signed status evidence without granting production authority.

    The envelope never supplies its own verification key.  A public key must be
    configured separately.  Even a valid signature remains non-authorizing
    because configuration alone does not prove independent governance.
    """

    def __init__(
        self,
        anchor: ConfiguredEd25519TrustAnchor | None = None,
        replay_protector: IndependentAuthorityReplayProtector | None = None,
    ) -> None:
        self._anchor = anchor
        self._replay = replay_protector or UnavailableIndependentAuthorityReplayProtector()

    def verify(
        self,
        *,
        request: PersonalKeyAuthorityRequest,
        envelope: SignedIndependentKeyStatusStatement | None,
        evaluated_at: datetime,
    ) -> IndependentTrustVerificationResult:
        now = _utc(evaluated_at)
        if self._anchor is None:
            return _failure(
                request=request,
                now=now,
                status=IndependentTrustVerificationStatus.UNAVAILABLE,
                codes=("INDEPENDENT_TRUST_ANCHOR_UNAVAILABLE",),
            )
        anchor = self._anchor
        if envelope is None:
            return _failure(
                request=request,
                now=now,
                status=IndependentTrustVerificationStatus.UNAVAILABLE,
                codes=("SIGNED_AUTHORITY_STATEMENT_UNAVAILABLE",),
                anchor=anchor,
            )

        statement = envelope.statement
        codes = _binding_codes(request=request, statement=statement, anchor=anchor)
        codes.extend(_time_codes(request=request, statement=statement, anchor=anchor, now=now))
        if codes:
            if "AUTHORITY_STATEMENT_SEQUENCE_ROLLBACK" in codes:
                status = IndependentTrustVerificationStatus.ROLLBACK
            elif any(code.endswith("STALE") or code.endswith("EXPIRED") for code in codes):
                status = IndependentTrustVerificationStatus.STALE
            else:
                status = IndependentTrustVerificationStatus.REJECTED
            return _failure(
                request=request,
                now=now,
                status=status,
                codes=tuple(codes),
                anchor=anchor,
                statement=statement,
            )

        try:
            key = serialization.load_pem_public_key(anchor.public_key_pem.encode("ascii"))
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("configured key is not Ed25519")
            key.verify(
                envelope.signature_bytes,
                canonical_independent_key_status_payload(statement),
            )
        except (InvalidSignature, TypeError, ValueError):
            return _failure(
                request=request,
                now=now,
                status=IndependentTrustVerificationStatus.REJECTED,
                codes=("AUTHORITY_STATEMENT_SIGNATURE_INVALID",),
                anchor=anchor,
                statement=statement,
            )
        except Exception:
            return _failure(
                request=request,
                now=now,
                status=IndependentTrustVerificationStatus.UNAVAILABLE,
                codes=("AUTHORITY_SIGNATURE_VERIFIER_UNAVAILABLE",),
                anchor=anchor,
                statement=statement,
            )

        binding = replay_binding_for(statement)
        try:
            replay = self._replay.check_and_record(binding=binding, checked_at=now)
        except Exception:
            replay = None
        if not isinstance(replay, IndependentReplayResult):
            return _failure(
                request=request,
                now=now,
                status=IndependentTrustVerificationStatus.UNAVAILABLE,
                codes=("EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE",),
                anchor=anchor,
                statement=statement,
                signature_verified=True,
            )
        if replay.binding_fingerprint != binding.binding_fingerprint or replay.checked_at != now:
            return _failure(
                request=request,
                now=now,
                status=IndependentTrustVerificationStatus.REJECTED,
                codes=("AUTHORITY_REPLAY_RESULT_MISMATCH",),
                anchor=anchor,
                statement=statement,
                signature_verified=True,
            )
        if replay.status is not IndependentReplayStatus.ACCEPTED:
            status = {
                IndependentReplayStatus.DUPLICATE: IndependentTrustVerificationStatus.REPLAYED,
                IndependentReplayStatus.ROLLBACK: IndependentTrustVerificationStatus.ROLLBACK,
                IndependentReplayStatus.CONFLICT: IndependentTrustVerificationStatus.CONFLICT,
                IndependentReplayStatus.UNAVAILABLE: IndependentTrustVerificationStatus.UNAVAILABLE,
            }[replay.status]
            return _failure(
                request=request,
                now=now,
                status=status,
                codes=replay.reason_codes,
                anchor=anchor,
                statement=statement,
                signature_verified=True,
            )

        status, reasons = _state_result(statement.key_state)
        return IndependentTrustVerificationResult(
            status=status,
            request_fingerprint=request.request_fingerprint,
            evaluated_at=now,
            anchor_fingerprint=anchor.public_key_fingerprint,
            statement_fingerprint=statement.statement_fingerprint,
            key_state=statement.key_state,
            signature_verified=True,
            replay_binding_verified=True,
            replay_locally_durable=replay.locally_durable,
            replay_atomic_consumption=replay.atomic_consumption,
            reason_codes=reasons,
        )


def _binding_codes(
    *,
    request: PersonalKeyAuthorityRequest,
    statement: IndependentKeyStatusStatement,
    anchor: ConfiguredEd25519TrustAnchor,
) -> list[str]:
    checks = (
        (anchor.authority_id, request.authority_id, "TRUST_ANCHOR_AUTHORITY_MISMATCH"),
        (anchor.anchor_id, request.trust_anchor_id, "TRUST_ANCHOR_ID_MISMATCH"),
        (anchor.anchor_version, request.trust_anchor_version, "TRUST_ANCHOR_VERSION_MISMATCH"),
        (anchor.public_key_fingerprint, request.trust_anchor_fingerprint, "TRUST_ANCHOR_FINGERPRINT_MISMATCH"),
        (statement.authority_id, request.authority_id, "AUTHORITY_STATEMENT_AUTHORITY_MISMATCH"),
        (statement.anchor_id, anchor.anchor_id, "AUTHORITY_STATEMENT_ANCHOR_ID_MISMATCH"),
        (statement.anchor_version, anchor.anchor_version, "AUTHORITY_STATEMENT_ANCHOR_VERSION_MISMATCH"),
        (statement.anchor_fingerprint, anchor.public_key_fingerprint, "AUTHORITY_STATEMENT_ANCHOR_FINGERPRINT_MISMATCH"),
        (statement.request_fingerprint, request.request_fingerprint, "AUTHORITY_STATEMENT_REQUEST_MISMATCH"),
        (statement.challenge_id, request.challenge_id, "AUTHORITY_STATEMENT_CHALLENGE_MISMATCH"),
        (statement.key_id, request.key_id, "AUTHORITY_STATEMENT_KEY_MISMATCH"),
        (statement.public_key_fingerprint, request.public_key_fingerprint, "AUTHORITY_STATEMENT_KEY_FINGERPRINT_MISMATCH"),
        (statement.key_generation, request.key_generation, "AUTHORITY_STATEMENT_GENERATION_MISMATCH"),
        (statement.scope, request.scope, "AUTHORITY_STATEMENT_SCOPE_CONTENT_MISMATCH"),
        (statement.scope_fingerprint, request.scope_fingerprint, "AUTHORITY_STATEMENT_SCOPE_MISMATCH"),
    )
    codes = [code for actual, expected, code in checks if actual != expected]
    if request.minimum_source_sequence is not None and statement.source_sequence <= request.minimum_source_sequence:
        codes.append("AUTHORITY_STATEMENT_SEQUENCE_ROLLBACK")
    return codes


def _time_codes(
    *,
    request: PersonalKeyAuthorityRequest,
    statement: IndependentKeyStatusStatement,
    anchor: ConfiguredEd25519TrustAnchor,
    now: datetime,
) -> list[str]:
    codes: list[str] = []
    if anchor.valid_from > now or (anchor.expires_at is not None and now >= anchor.expires_at):
        codes.append("TRUST_ANCHOR_EXPIRED")
    if statement.issued_at < anchor.valid_from:
        codes.append("AUTHORITY_STATEMENT_PREDATES_ANCHOR")
    if statement.issued_at < request.requested_at:
        codes.append("AUTHORITY_STATEMENT_PREDATES_REQUEST")
    if statement.issued_at > now or statement.observed_at > now:
        codes.append("AUTHORITY_STATEMENT_FROM_FUTURE")
    if now >= statement.expires_at:
        codes.append("AUTHORITY_STATEMENT_EXPIRED")
    if now - statement.observed_at > timedelta(seconds=request.max_age_seconds):
        codes.append("AUTHORITY_STATEMENT_STALE")
    return codes


def _state_result(
    state: PersonalKeyAuthorityState,
) -> tuple[IndependentTrustVerificationStatus, tuple[str, ...]]:
    return {
        PersonalKeyAuthorityState.ACTIVE: (
            IndependentTrustVerificationStatus.CRYPTOGRAPHICALLY_VERIFIED,
            (),
        ),
        PersonalKeyAuthorityState.REVOKED: (
            IndependentTrustVerificationStatus.REVOKED,
            ("AUTHORITY_KEY_REVOKED",),
        ),
        PersonalKeyAuthorityState.SUPERSEDED: (
            IndependentTrustVerificationStatus.SUPERSEDED,
            ("AUTHORITY_KEY_SUPERSEDED",),
        ),
        PersonalKeyAuthorityState.COMPROMISED: (
            IndependentTrustVerificationStatus.COMPROMISED,
            ("AUTHORITY_KEY_COMPROMISED",),
        ),
        PersonalKeyAuthorityState.RECOVERY_REQUIRED: (
            IndependentTrustVerificationStatus.RECOVERY_REQUIRED,
            ("AUTHORITY_KEY_RECOVERY_REQUIRED",),
        ),
    }[state]


def _failure(
    *,
    request: PersonalKeyAuthorityRequest,
    now: datetime,
    status: IndependentTrustVerificationStatus,
    codes: tuple[str, ...],
    anchor: ConfiguredEd25519TrustAnchor | None = None,
    statement: IndependentKeyStatusStatement | None = None,
    signature_verified: bool = False,
) -> IndependentTrustVerificationResult:
    return IndependentTrustVerificationResult(
        status=status,
        request_fingerprint=request.request_fingerprint,
        evaluated_at=now,
        anchor_fingerprint=None if anchor is None else anchor.public_key_fingerprint,
        statement_fingerprint=None if statement is None else statement.statement_fingerprint,
        key_state=None if statement is None else statement.key_state,
        signature_verified=signature_verified,
        reason_codes=tuple(sorted(set(codes))),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trust-verification time requires timezone awareness")
    return value.astimezone(UTC)
