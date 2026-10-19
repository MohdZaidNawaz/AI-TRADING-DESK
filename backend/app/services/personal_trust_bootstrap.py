"""Read-only personal trust bootstrap, key-status, and recovery diagnostics.

The repository has no independently authenticated personal-key authority.
Synthetic providers can exercise exact binding and failure behavior in tests,
but all outputs remain structurally non-authorizing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.models.independent_governance import ExternalAuthorityAvailability
from app.models.personal_policy_signing import PersonalKeyRecoveryStatus, PersonalPublicKeyDescriptor
from app.models.personal_trust_bootstrap import (
    PersonalAuthorityReplayResult,
    PersonalAuthorityReplayStatus,
    PersonalKeyAuthorityAssessment,
    PersonalKeyAuthorityAssessmentStatus,
    PersonalKeyAuthorityObservation,
    PersonalKeyAuthorityRequest,
    PersonalKeyAuthorityState,
    PersonalKeyRecoveryAssessment,
    PersonalRecoveryState,
    PersonalTrustBootstrapAssessment,
    PersonalTrustBootstrapCeremony,
    PersonalTrustBootstrapStatus,
    PersonalTrustDecision,
    PersonalTrustDecisionStatus,
    PersonalTrustScope,
)


@runtime_checkable
class ReadOnlyPersonalKeyAuthorityProvider(Protocol):
    """External key-status seam; protocol conformance is not a trust boundary."""

    @property
    def provider_id(self) -> str: ...

    def observe(self, request: PersonalKeyAuthorityRequest) -> PersonalKeyAuthorityObservation: ...


@runtime_checkable
class PersonalKeyAuthorityReplayProtector(Protocol):
    """Durable replay seam; a caller implementation cannot create authority."""

    def check_and_record(
        self,
        *,
        provider_id: str,
        authority_id: str,
        key_id: str,
        source_event_id: str,
        source_sequence: int,
        observation_fingerprint: str,
        checked_at: datetime,
    ) -> PersonalAuthorityReplayResult: ...


class UnavailablePersonalKeyAuthorityProvider:
    provider_id = "UNCONFIGURED_PERSONAL_KEY_AUTHORITY"

    def observe(self, request: PersonalKeyAuthorityRequest) -> PersonalKeyAuthorityObservation:
        return PersonalKeyAuthorityObservation(
            availability=ExternalAuthorityAvailability.UNAVAILABLE,
            provider_id=self.provider_id,
            request_fingerprint=request.request_fingerprint,
            challenge_id=request.challenge_id,
            retrieved_at=request.requested_at,
            reason_codes=("PERSONAL_KEY_AUTHORITY_UNAVAILABLE",),
        )


class UnavailablePersonalKeyAuthorityReplayProtector:
    def check_and_record(
        self,
        *,
        provider_id: str,
        authority_id: str,
        key_id: str,
        source_event_id: str,
        source_sequence: int,
        observation_fingerprint: str,
        checked_at: datetime,
    ) -> PersonalAuthorityReplayResult:
        return PersonalAuthorityReplayResult(
            status=PersonalAuthorityReplayStatus.UNAVAILABLE,
            provider_id=provider_id,
            authority_id=authority_id,
            key_id=key_id,
            source_event_id=source_event_id,
            source_sequence=source_sequence,
            observation_fingerprint=observation_fingerprint,
            checked_at=checked_at,
            reason_codes=("PERSONAL_KEY_REPLAY_PROTECTION_UNAVAILABLE",),
        )


class PersonalKeyAuthorityIntegration:
    """Validate key-status claims without promoting a provider to authority."""

    def __init__(
        self,
        provider: ReadOnlyPersonalKeyAuthorityProvider | None = None,
        replay_protector: PersonalKeyAuthorityReplayProtector | None = None,
    ) -> None:
        self._provider = provider or UnavailablePersonalKeyAuthorityProvider()
        self._replay = replay_protector or UnavailablePersonalKeyAuthorityReplayProtector()

    def assess(
        self, *, request: PersonalKeyAuthorityRequest, evaluated_at: datetime
    ) -> PersonalKeyAuthorityAssessment:
        now = _utc(evaluated_at)
        provider_id = _provider_id(self._provider)
        try:
            observation = self._provider.observe(request)
        except Exception:
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=PersonalKeyAuthorityAssessmentStatus.UNAVAILABLE,
                codes=("PERSONAL_KEY_AUTHORITY_PROVIDER_ERROR",),
            )
        if not isinstance(observation, PersonalKeyAuthorityObservation):
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=PersonalKeyAuthorityAssessmentStatus.REJECTED,
                codes=("PERSONAL_KEY_AUTHORITY_RESULT_INVALID",),
            )

        codes: list[str] = []
        if observation.provider_id != provider_id:
            codes.append("PERSONAL_KEY_AUTHORITY_PROVIDER_MISMATCH")
        if observation.request_fingerprint != request.request_fingerprint:
            codes.append("PERSONAL_KEY_AUTHORITY_REQUEST_MISMATCH")
        if observation.challenge_id != request.challenge_id:
            codes.append("PERSONAL_KEY_AUTHORITY_CHALLENGE_MISMATCH")
        codes.extend(_observation_time_codes(request=request, observation=observation, now=now))

        if observation.availability is not ExternalAuthorityAvailability.AVAILABLE:
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=(
                    PersonalKeyAuthorityAssessmentStatus.CONFLICT
                    if observation.availability is ExternalAuthorityAvailability.CONFLICT
                    else PersonalKeyAuthorityAssessmentStatus.UNAVAILABLE
                ),
                codes=tuple(codes) + observation.reason_codes,
                observation=observation,
            )

        exact_bindings = (
            (observation.authority_id, request.authority_id, "PERSONAL_KEY_AUTHORITY_ID_MISMATCH"),
            (
                observation.trust_anchor_id,
                request.trust_anchor_id,
                "PERSONAL_KEY_TRUST_ANCHOR_ID_MISMATCH",
            ),
            (
                observation.trust_anchor_version,
                request.trust_anchor_version,
                "PERSONAL_KEY_TRUST_ANCHOR_VERSION_MISMATCH",
            ),
            (
                observation.trust_anchor_fingerprint,
                request.trust_anchor_fingerprint,
                "PERSONAL_KEY_TRUST_ANCHOR_MISMATCH",
            ),
            (observation.key_id, request.key_id, "PERSONAL_KEY_ID_MISMATCH"),
            (
                observation.public_key_fingerprint,
                request.public_key_fingerprint,
                "PERSONAL_KEY_FINGERPRINT_MISMATCH",
            ),
            (
                observation.key_generation,
                request.key_generation,
                "PERSONAL_KEY_GENERATION_MISMATCH",
            ),
            (observation.scope, request.scope, "PERSONAL_KEY_SCOPE_CONTENT_MISMATCH"),
            (
                observation.scope_fingerprint,
                request.scope_fingerprint,
                "PERSONAL_KEY_SCOPE_MISMATCH",
            ),
        )
        for actual, expected, code in exact_bindings:
            if actual != expected:
                codes.append(code)
        if not observation.source_authenticated:
            codes.append("PERSONAL_KEY_AUTHORITY_UNAUTHENTICATED")
        if not observation.trust_anchor_validated:
            codes.append("PERSONAL_KEY_TRUST_ANCHOR_UNVERIFIED")
        if (
            request.minimum_source_sequence is not None
            and observation.source_sequence is not None
            and observation.source_sequence <= request.minimum_source_sequence
        ):
            codes.append("PERSONAL_KEY_AUTHORITY_SEQUENCE_ROLLBACK")

        if codes:
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=(
                    PersonalKeyAuthorityAssessmentStatus.STALE
                    if any(code.endswith("STALE") or code.endswith("EXPIRED") for code in codes)
                    else PersonalKeyAuthorityAssessmentStatus.REPLAYED
                    if "PERSONAL_KEY_AUTHORITY_SEQUENCE_ROLLBACK" in codes
                    else PersonalKeyAuthorityAssessmentStatus.REJECTED
                ),
                codes=tuple(codes),
                observation=observation,
            )

        assert observation.authority_id is not None
        assert observation.key_id is not None
        assert observation.source_event_id is not None
        assert observation.source_sequence is not None
        try:
            replay = self._replay.check_and_record(
                provider_id=provider_id,
                authority_id=observation.authority_id,
                key_id=observation.key_id,
                source_event_id=observation.source_event_id,
                source_sequence=observation.source_sequence,
                observation_fingerprint=observation.observation_fingerprint,
                checked_at=now,
            )
        except Exception:
            replay = None
        if not isinstance(replay, PersonalAuthorityReplayResult):
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=PersonalKeyAuthorityAssessmentStatus.UNAVAILABLE,
                codes=("PERSONAL_KEY_REPLAY_PROTECTION_UNAVAILABLE",),
                observation=observation,
            )
        replay_matches = (
            replay.provider_id == provider_id
            and replay.authority_id == observation.authority_id
            and replay.key_id == observation.key_id
            and replay.source_event_id == observation.source_event_id
            and replay.source_sequence == observation.source_sequence
            and replay.observation_fingerprint == observation.observation_fingerprint
            and replay.checked_at == now
        )
        if not replay_matches:
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=PersonalKeyAuthorityAssessmentStatus.REJECTED,
                codes=("PERSONAL_KEY_REPLAY_RESULT_MISMATCH",),
                observation=observation,
            )
        if replay.status is not PersonalAuthorityReplayStatus.ACCEPTED:
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status={
                    PersonalAuthorityReplayStatus.DUPLICATE: PersonalKeyAuthorityAssessmentStatus.REPLAYED,
                    PersonalAuthorityReplayStatus.ROLLBACK: PersonalKeyAuthorityAssessmentStatus.REPLAYED,
                    PersonalAuthorityReplayStatus.CONFLICT: PersonalKeyAuthorityAssessmentStatus.CONFLICT,
                    PersonalAuthorityReplayStatus.UNAVAILABLE: PersonalKeyAuthorityAssessmentStatus.UNAVAILABLE,
                    PersonalAuthorityReplayStatus.ACCEPTED: PersonalKeyAuthorityAssessmentStatus.REJECTED,
                }[replay.status],
                codes=replay.reason_codes,
                observation=observation,
            )

        assert observation.key_state is not None
        status = {
            PersonalKeyAuthorityState.ACTIVE: PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED,
            PersonalKeyAuthorityState.REVOKED: PersonalKeyAuthorityAssessmentStatus.REVOKED,
            PersonalKeyAuthorityState.SUPERSEDED: PersonalKeyAuthorityAssessmentStatus.SUPERSEDED,
            PersonalKeyAuthorityState.COMPROMISED: PersonalKeyAuthorityAssessmentStatus.COMPROMISED,
            PersonalKeyAuthorityState.RECOVERY_REQUIRED: PersonalKeyAuthorityAssessmentStatus.RECOVERY_REQUIRED,
        }[observation.key_state]
        reasons = {
            PersonalKeyAuthorityState.ACTIVE: (),
            PersonalKeyAuthorityState.REVOKED: ("PERSONAL_KEY_REVOKED",),
            PersonalKeyAuthorityState.SUPERSEDED: ("PERSONAL_KEY_SUPERSEDED",),
            PersonalKeyAuthorityState.COMPROMISED: ("PERSONAL_KEY_COMPROMISED",),
            PersonalKeyAuthorityState.RECOVERY_REQUIRED: ("PERSONAL_KEY_RECOVERY_REQUIRED",),
        }[observation.key_state]
        return PersonalKeyAuthorityAssessment(
            status=status,
            provider_id=provider_id,
            evaluated_at=now,
            **_request_lineage(request),
            observation_fingerprint=observation.observation_fingerprint,
            source_event_id=observation.source_event_id,
            source_sequence=observation.source_sequence,
            observed_at=observation.observed_at,
            retrieved_at=observation.retrieved_at,
            observation_expires_at=observation.expires_at,
            key_state=observation.key_state,
            replacement_key_id=observation.replacement_key_id,
            replacement_key_fingerprint=observation.replacement_key_fingerprint,
            replacement_generation=observation.replacement_generation,
            reason_codes=reasons,
            source_authentication_claim_validated=True,
            replay_check_passed=True,
        )


class PersonalTrustBootstrapService:
    """Assess a local ceremony without turning local records into authority."""

    def assess(
        self,
        *,
        ceremony: PersonalTrustBootstrapCeremony,
        decision: PersonalTrustDecision | None,
        local_recovery: PersonalKeyRecoveryStatus,
        authority: PersonalKeyAuthorityAssessment | None,
        evaluated_at: datetime,
        authority_request: PersonalKeyAuthorityRequest | None = None,
    ) -> PersonalTrustBootstrapAssessment:
        now = _utc(evaluated_at)
        codes: list[str] = []
        actions: list[str] = []
        status = PersonalTrustBootstrapStatus.PENDING_HUMAN_CONFIRMATION
        confirmed = False
        decision_fingerprint: str | None = None

        if now < ceremony.presented_at or now >= ceremony.expires_at:
            codes.append("PERSONAL_TRUST_CEREMONY_EXPIRED_OR_NOT_EFFECTIVE")
            actions.append("START_NEW_PERSONAL_TRUST_CEREMONY")
            status = PersonalTrustBootstrapStatus.INVALID
        elif decision is None or decision.status is PersonalTrustDecisionStatus.PENDING:
            codes.append("HUMAN_FINGERPRINT_VERIFICATION_PENDING")
            actions.append("VERIFY_PUBLIC_KEY_FINGERPRINT_OUT_OF_BAND")
        else:
            decision_fingerprint = decision.fingerprint
            bindings_match = (
                decision.ceremony_fingerprint == ceremony.fingerprint
                and decision.key_id == ceremony.descriptor.key_id
                and decision.public_key_fingerprint == ceremony.expected_public_key_fingerprint
                and decision.scope_fingerprint == ceremony.scope.fingerprint
                and decision.authority_id == ceremony.expected_authority_id
                and decision.trust_anchor_id == ceremony.trust_anchor_id
                and decision.trust_anchor_version == ceremony.trust_anchor_version
                and decision.trust_anchor_fingerprint == ceremony.trust_anchor_fingerprint
                and decision.ceremony_presented_at == ceremony.presented_at
                and decision.ceremony_expires_at == ceremony.expires_at
            )
            if not bindings_match:
                codes.append("PERSONAL_TRUST_DECISION_BINDING_MISMATCH")
                actions.append("REJECT_AND_RESTART_PERSONAL_TRUST_CEREMONY")
                status = PersonalTrustBootstrapStatus.INVALID
            elif decision.status is PersonalTrustDecisionStatus.REJECTED:
                codes.extend(decision.reason_codes or ("PERSONAL_TRUST_DECISION_REJECTED",))
                actions.append("KEEP_PERSONAL_KEY_UNTRUSTED")
                status = PersonalTrustBootstrapStatus.REJECTED
            elif decision.decided_at is None or decision.decided_at > now:
                codes.append("PERSONAL_TRUST_DECISION_TIME_INVALID")
                actions.append("REJECT_AND_RESTART_PERSONAL_TRUST_CEREMONY")
                status = PersonalTrustBootstrapStatus.INVALID
            else:
                confirmed = True
                status = PersonalTrustBootstrapStatus.LOCAL_CONFIRMATION_RECORDED

        local_available = (
            local_recovery.key_id == ceremony.descriptor.key_id
            and local_recovery.metadata_available
            and local_recovery.signing_capability_available
            and not local_recovery.requires_external_recovery
        )
        if not local_available:
            codes.append("LOCAL_PERSONAL_KEY_UNAVAILABLE_OR_MISMATCHED")
            actions.append("RESOLVE_LOCAL_KEY_RECOVERY_WITHOUT_REPLACEMENT")

        authority_status: PersonalKeyAuthorityAssessmentStatus | None = None
        if authority is None:
            codes.append("AUTHORITATIVE_PERSONAL_KEY_STATUS_UNAVAILABLE")
            actions.append("CONFIGURE_INDEPENDENT_PERSONAL_KEY_STATUS_AUTHORITY")
        elif authority_request is None:
            codes.append("PERSONAL_KEY_AUTHORITY_REQUEST_CONTEXT_MISSING")
            actions.append("REVERIFY_PERSONAL_KEY_STATUS_FOR_EXACT_CEREMONY")
            authority_status = PersonalKeyAuthorityAssessmentStatus.REJECTED
        else:
            binding_codes = _authority_subject_codes(
                authority=authority,
                request=authority_request,
                descriptor=ceremony.descriptor,
                scope=ceremony.scope,
                expected_authority_id=ceremony.expected_authority_id,
                expected_trust_anchor_id=ceremony.trust_anchor_id,
                expected_trust_anchor_version=ceremony.trust_anchor_version,
                expected_trust_anchor_fingerprint=ceremony.trust_anchor_fingerprint,
                now=now,
            )
            if binding_codes:
                codes.extend(binding_codes)
                actions.append("REVERIFY_PERSONAL_KEY_STATUS_FOR_EXACT_CEREMONY")
                authority_status = PersonalKeyAuthorityAssessmentStatus.REJECTED
            else:
                authority_status = authority.status
            if (
                authority_status is not PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED
                and not binding_codes
            ):
                codes.extend(authority.reason_codes or ("AUTHORITATIVE_PERSONAL_KEY_STATUS_UNAVAILABLE",))
            # Phase 13K has no genuine authentication trust boundary. Even a
            # structurally valid synthetic observation cannot satisfy this.
            codes.append("INDEPENDENT_PERSONAL_KEY_AUTHORITY_NOT_ESTABLISHED")
            actions.append("ESTABLISH_INDEPENDENT_PERSONAL_KEY_AUTHORITY")

        codes.append("DURABLE_PERSONAL_KEY_REPLAY_PROTECTION_NOT_ESTABLISHED")
        actions.append("ESTABLISH_DURABLE_AUTHORITY_REPLAY_PROTECTION")
        return PersonalTrustBootstrapAssessment(
            status=status,
            ceremony_fingerprint=ceremony.fingerprint,
            key_id=ceremony.descriptor.key_id,
            public_key_fingerprint=ceremony.expected_public_key_fingerprint,
            scope_fingerprint=ceremony.scope.fingerprint,
            evaluated_at=now,
            decision_fingerprint=decision_fingerprint if confirmed else None,
            local_confirmation_recorded=confirmed,
            local_key_available=local_available,
            authority_assessment_status=authority_status,
            reason_codes=tuple(codes),
            required_actions=tuple(actions),
        )

    def assess_recovery(
        self,
        *,
        local_recovery: PersonalKeyRecoveryStatus,
        authority: PersonalKeyAuthorityAssessment | None,
        evaluated_at: datetime,
        authority_request: PersonalKeyAuthorityRequest | None = None,
        descriptor: PersonalPublicKeyDescriptor | None = None,
        scope: PersonalTrustScope | None = None,
        expected_authority_id: str | None = None,
        expected_trust_anchor_id: str | None = None,
        expected_trust_anchor_version: str | None = None,
        expected_trust_anchor_fingerprint: str | None = None,
        suspected_compromise: bool = False,
        interrupted_operation: bool = False,
        rollback_detected: bool = False,
    ) -> PersonalKeyRecoveryAssessment:
        now = _utc(evaluated_at)
        state: PersonalRecoveryState
        codes: tuple[str, ...]
        actions: tuple[str, ...]
        authority_status = None if authority is None else authority.status
        authority_binding_codes: tuple[str, ...] = ()
        if authority is not None:
            if (
                authority_request is None
                or descriptor is None
                or scope is None
                or expected_authority_id is None
                or expected_trust_anchor_id is None
                or expected_trust_anchor_version is None
                or expected_trust_anchor_fingerprint is None
            ):
                authority_binding_codes = ("PERSONAL_KEY_AUTHORITY_RECOVERY_CONTEXT_MISSING",)
            else:
                authority_binding_codes = _authority_subject_codes(
                    authority=authority,
                    request=authority_request,
                    descriptor=descriptor,
                    scope=scope,
                    expected_authority_id=expected_authority_id,
                    expected_trust_anchor_id=expected_trust_anchor_id,
                    expected_trust_anchor_version=expected_trust_anchor_version,
                    expected_trust_anchor_fingerprint=expected_trust_anchor_fingerprint,
                    now=now,
                )
                if authority_request.key_id != local_recovery.key_id:
                    authority_binding_codes += ("PERSONAL_KEY_AUTHORITY_LOCAL_KEY_MISMATCH",)
            if authority_binding_codes:
                authority_status = PersonalKeyAuthorityAssessmentStatus.REJECTED

        if rollback_detected:
            state = PersonalRecoveryState.ROLLBACK_DETECTED
            codes = ("PERSONAL_KEY_LOCAL_STATE_ROLLBACK_DETECTED",)
            actions = ("REQUIRE_NEW_TRUST_DECISION_AFTER_ROLLBACK",)
        elif suspected_compromise:
            state = PersonalRecoveryState.SUSPECTED_COMPROMISE
            codes = ("PERSONAL_KEY_COMPROMISE_SUSPECTED",)
            actions = ("REVOKE_AND_ESTABLISH_NEW_TRUST_CHAIN",)
        elif authority_binding_codes:
            state = PersonalRecoveryState.AUTHORITY_UNAVAILABLE
            codes = tuple(sorted(set(authority_binding_codes)))
            actions = ("REVERIFY_PERSONAL_KEY_STATUS_FOR_EXACT_RECOVERY_SUBJECT",)
        elif authority_status is PersonalKeyAuthorityAssessmentStatus.REVOKED:
            state = PersonalRecoveryState.REVOKED
            codes = ("PERSONAL_KEY_REVOKED",)
            actions = ("KEEP_REVOKED_KEY_INACTIVE", "ESTABLISH_NEW_TRUST_CHAIN")
        elif authority_status is PersonalKeyAuthorityAssessmentStatus.SUPERSEDED:
            state = PersonalRecoveryState.SUPERSEDED
            codes = ("PERSONAL_KEY_SUPERSEDED",)
            actions = ("VERIFY_REPLACEMENT_KEY_OUT_OF_BAND",)
        elif authority_status is PersonalKeyAuthorityAssessmentStatus.COMPROMISED:
            state = PersonalRecoveryState.SUSPECTED_COMPROMISE
            codes = ("PERSONAL_KEY_COMPROMISED",)
            actions = ("REVOKE_AND_ESTABLISH_NEW_TRUST_CHAIN",)
        elif authority_status is PersonalKeyAuthorityAssessmentStatus.RECOVERY_REQUIRED:
            state = PersonalRecoveryState.AUTHORITY_RECOVERY_REQUIRED
            codes = ("AUTHORITATIVE_PERSONAL_KEY_RECOVERY_REQUIRED",)
            actions = ("COMPLETE_AUTHORITY_DIRECTED_RECOVERY_AND_NEW_TRUST_DECISION",)
        elif interrupted_operation:
            state = PersonalRecoveryState.INTERRUPTED
            codes = ("PERSONAL_KEY_LIFECYCLE_OPERATION_INTERRUPTED",)
            actions = ("RECONCILE_KEY_LIFECYCLE_BEFORE_USE",)
        elif not local_recovery.metadata_available:
            state = PersonalRecoveryState.KEY_LOST
            codes = ("PERSONAL_KEY_RECORD_MISSING",)
            actions = ("REQUIRE_NEW_TRUST_DECISION_FOR_REPLACEMENT",)
        elif not local_recovery.signing_capability_available:
            state = PersonalRecoveryState.KEY_CORRUPT_OR_UNDECRYPTABLE
            codes = tuple(local_recovery.reason_codes) or ("PERSONAL_KEY_UNDECRYPTABLE",)
            actions = ("PRESERVE_FAILED_RECORD_AND_ESTABLISH_NEW_TRUST_CHAIN",)
        elif authority is None or authority_status in {
            PersonalKeyAuthorityAssessmentStatus.UNAVAILABLE,
            PersonalKeyAuthorityAssessmentStatus.REJECTED,
            PersonalKeyAuthorityAssessmentStatus.STALE,
            PersonalKeyAuthorityAssessmentStatus.REPLAYED,
            PersonalKeyAuthorityAssessmentStatus.CONFLICT,
        }:
            state = PersonalRecoveryState.AUTHORITY_UNAVAILABLE
            codes = (
                tuple(authority.reason_codes)
                if authority is not None and authority.reason_codes
                else ("AUTHORITATIVE_PERSONAL_KEY_STATUS_UNAVAILABLE",)
            )
            actions = ("RESTORE_AUTHORITATIVE_KEY_STATUS_BEFORE_USE",)
        else:
            state = PersonalRecoveryState.LOCAL_KEY_AVAILABLE
            codes = ("INDEPENDENT_PERSONAL_KEY_AUTHORITY_NOT_ESTABLISHED",)
            actions = ("COMPLETE_INDEPENDENT_TRUST_AND_REVOCATION_SETUP",)

        return PersonalKeyRecoveryAssessment(
            state=state,
            key_id=local_recovery.key_id,
            evaluated_at=now,
            local_recovery=local_recovery,
            authority_assessment_status=authority_status,
            reason_codes=codes,
            required_actions=actions,
        )


def _observation_time_codes(
    *,
    request: PersonalKeyAuthorityRequest,
    observation: PersonalKeyAuthorityObservation,
    now: datetime,
) -> tuple[str, ...]:
    codes: list[str] = []
    if request.requested_at > now:
        codes.append("PERSONAL_KEY_AUTHORITY_REQUEST_IN_FUTURE")
    if observation.retrieved_at < request.requested_at or observation.retrieved_at > now:
        codes.append("PERSONAL_KEY_AUTHORITY_RETRIEVAL_TIME_INVALID")
    if observation.observed_at is not None:
        if observation.observed_at > observation.retrieved_at:
            codes.append("PERSONAL_KEY_AUTHORITY_OBSERVATION_TIME_INVALID")
        elif (now - observation.observed_at).total_seconds() > request.max_age_seconds:
            codes.append("PERSONAL_KEY_AUTHORITY_OBSERVATION_STALE")
    if observation.expires_at is not None and now >= observation.expires_at:
        codes.append("PERSONAL_KEY_AUTHORITY_OBSERVATION_EXPIRED")
    return tuple(codes)


def _authority_failure(
    *,
    request: PersonalKeyAuthorityRequest,
    provider_id: str,
    now: datetime,
    status: PersonalKeyAuthorityAssessmentStatus,
    codes: tuple[str, ...],
    observation: PersonalKeyAuthorityObservation | None = None,
) -> PersonalKeyAuthorityAssessment:
    return PersonalKeyAuthorityAssessment(
        status=status,
        provider_id=provider_id,
        evaluated_at=now,
        **_request_lineage(request),
        observation_fingerprint=(observation.observation_fingerprint if observation else None),
        source_event_id=(observation.source_event_id if observation else None),
        source_sequence=(observation.source_sequence if observation else None),
        observed_at=(observation.observed_at if observation else None),
        retrieved_at=(observation.retrieved_at if observation else None),
        observation_expires_at=(observation.expires_at if observation else None),
        reason_codes=tuple(codes) or ("PERSONAL_KEY_AUTHORITY_UNAVAILABLE",),
    )


def _request_lineage(request: PersonalKeyAuthorityRequest) -> dict[str, object]:
    return {
        "request_fingerprint": request.request_fingerprint,
        "authority_id": request.authority_id,
        "trust_anchor_id": request.trust_anchor_id,
        "trust_anchor_version": request.trust_anchor_version,
        "trust_anchor_fingerprint": request.trust_anchor_fingerprint,
        "key_id": request.key_id,
        "public_key_fingerprint": request.public_key_fingerprint,
        "key_generation": request.key_generation,
        "scope": request.scope,
        "scope_fingerprint": request.scope_fingerprint,
        "challenge_id": request.challenge_id,
        "requested_at": request.requested_at,
        "max_age_seconds": request.max_age_seconds,
        "minimum_source_sequence": request.minimum_source_sequence,
    }


def _authority_subject_codes(
    *,
    authority: PersonalKeyAuthorityAssessment,
    request: PersonalKeyAuthorityRequest,
    descriptor: PersonalPublicKeyDescriptor,
    scope: PersonalTrustScope,
    expected_authority_id: str,
    expected_trust_anchor_id: str,
    expected_trust_anchor_version: str,
    expected_trust_anchor_fingerprint: str,
    now: datetime,
) -> tuple[str, ...]:
    codes: list[str] = []
    for field, expected in _request_lineage(request).items():
        if getattr(authority, field) != expected:
            codes.append(f"PERSONAL_KEY_AUTHORITY_ASSESSMENT_{field.upper()}_MISMATCH")
    request_bindings = (
        (request.key_id, descriptor.key_id, "PERSONAL_KEY_REQUEST_KEY_MISMATCH"),
        (
            request.public_key_fingerprint,
            descriptor.public_key_fingerprint,
            "PERSONAL_KEY_REQUEST_FINGERPRINT_MISMATCH",
        ),
        (
            request.key_generation,
            descriptor.generation,
            "PERSONAL_KEY_REQUEST_GENERATION_MISMATCH",
        ),
        (request.scope, scope, "PERSONAL_KEY_REQUEST_SCOPE_MISMATCH"),
        (
            request.scope_fingerprint,
            scope.fingerprint,
            "PERSONAL_KEY_REQUEST_SCOPE_FINGERPRINT_MISMATCH",
        ),
        (request.authority_id, expected_authority_id, "PERSONAL_KEY_REQUEST_AUTHORITY_MISMATCH"),
        (
            request.trust_anchor_id,
            expected_trust_anchor_id,
            "PERSONAL_KEY_REQUEST_TRUST_ANCHOR_ID_MISMATCH",
        ),
        (
            request.trust_anchor_version,
            expected_trust_anchor_version,
            "PERSONAL_KEY_REQUEST_TRUST_ANCHOR_VERSION_MISMATCH",
        ),
        (
            request.trust_anchor_fingerprint,
            expected_trust_anchor_fingerprint,
            "PERSONAL_KEY_REQUEST_TRUST_ANCHOR_FINGERPRINT_MISMATCH",
        ),
    )
    for actual, expected, code in request_bindings:
        if actual != expected:
            codes.append(code)
    validated_statuses = {
        PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED,
        PersonalKeyAuthorityAssessmentStatus.REVOKED,
        PersonalKeyAuthorityAssessmentStatus.SUPERSEDED,
        PersonalKeyAuthorityAssessmentStatus.COMPROMISED,
        PersonalKeyAuthorityAssessmentStatus.RECOVERY_REQUIRED,
    }
    if authority.status in validated_statuses:
        if not authority.source_authentication_claim_validated:
            codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_AUTHENTICATION_MISSING")
        if not authority.replay_check_passed:
            codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_REPLAY_CHECK_MISSING")
        if any(
            value is None
            for value in (
                authority.observation_fingerprint,
                authority.source_event_id,
                authority.source_sequence,
                authority.observed_at,
                authority.retrieved_at,
                authority.observation_expires_at,
                authority.key_state,
            )
        ):
            codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_LINEAGE_INCOMPLETE")
        expected_state = {
            PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED: PersonalKeyAuthorityState.ACTIVE,
            PersonalKeyAuthorityAssessmentStatus.REVOKED: PersonalKeyAuthorityState.REVOKED,
            PersonalKeyAuthorityAssessmentStatus.SUPERSEDED: PersonalKeyAuthorityState.SUPERSEDED,
            PersonalKeyAuthorityAssessmentStatus.COMPROMISED: PersonalKeyAuthorityState.COMPROMISED,
            PersonalKeyAuthorityAssessmentStatus.RECOVERY_REQUIRED: PersonalKeyAuthorityState.RECOVERY_REQUIRED,
        }[authority.status]
        if authority.key_state is not expected_state:
            codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_STATE_MISMATCH")
    if (
        request.minimum_source_sequence is not None
        and (
            authority.source_sequence is None
            or authority.source_sequence <= request.minimum_source_sequence
        )
    ):
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_SEQUENCE_ROLLBACK")
    if authority.evaluated_at > now or authority.evaluated_at < request.requested_at:
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID")
    if (
        authority.observed_at is not None
        and authority.retrieved_at is not None
        and authority.observed_at > authority.retrieved_at
    ):
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID")
    if authority.retrieved_at is not None and (
        authority.retrieved_at < request.requested_at
        or authority.retrieved_at > authority.evaluated_at
    ):
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID")
    if (
        authority.observed_at is not None
        and authority.observation_expires_at is not None
        and authority.observation_expires_at <= authority.observed_at
    ):
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID")
    if authority.observed_at is not None and (
        authority.observed_at > now
        or (now - authority.observed_at).total_seconds() > request.max_age_seconds
    ):
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_STALE")
    if authority.observation_expires_at is not None and now >= authority.observation_expires_at:
        codes.append("PERSONAL_KEY_AUTHORITY_ASSESSMENT_EXPIRED")
    return tuple(sorted(set(codes)))


def _provider_id(provider: object) -> str:
    try:
        value = getattr(provider, "provider_id", "")
    except Exception:
        return "INVALID_PERSONAL_KEY_AUTHORITY_PROVIDER"
    normalized = str(value).strip().upper()
    return normalized or "INVALID_PERSONAL_KEY_AUTHORITY_PROVIDER"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("personal trust evaluation time requires timezone awareness")
    return value.astimezone(UTC)
