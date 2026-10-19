"""Phase 13K personal-trust tests use synthetic, non-authoritative sources."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import inspect

import pytest
from pydantic import ValidationError

from app.models.independent_governance import ExternalAuthorityAvailability
from app.models.personal_policy_signing import (
    PersonalKeyProtection,
    PersonalKeyRecoveryStatus,
    PersonalPublicKeyDescriptor,
)
from app.models.personal_trust_bootstrap import (
    PersonalAuthorityReplayResult,
    PersonalAuthorityReplayStatus,
    PersonalKeyAuthorityAssessment,
    PersonalKeyAuthorityAssessmentStatus,
    PersonalKeyAuthorityObservation,
    PersonalKeyAuthorityRequest,
    PersonalKeyAuthorityState,
    PersonalRecoveryState,
    PersonalTrustBootstrapCeremony,
    PersonalTrustBootstrapStatus,
    PersonalTrustDecision,
    PersonalTrustDecisionStatus,
    PersonalTrustScope,
    PersonalTrustVerificationMethod,
)
from app.models.production_authorization import (
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.services import personal_trust_bootstrap as bootstrap_module
from app.services.personal_trust_bootstrap import (
    PersonalKeyAuthorityIntegration,
    PersonalTrustBootstrapService,
)
from app.services.production_authorization_readiness import ProductionAuthorizationReadinessGate


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAERERERERERERERERERERERERERERERERERERERERERE=
-----END PUBLIC KEY-----
"""


def _descriptor(**changes: object) -> PersonalPublicKeyDescriptor:
    values: dict[str, object] = {
        "key_id": "personal-key-1",
        "public_key_pem": PUBLIC_KEY,
        "generation": 1,
        "created_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=1),
        "protection": PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER,
    }
    values.update(changes)
    return PersonalPublicKeyDescriptor(**values)


def _scope(**changes: object) -> PersonalTrustScope:
    values: dict[str, object] = {
        "scope_id": "personal-xauusd-demo",
        "account_id": "demo-123",
        "execution_mode": "disabled",
        "canonical_symbol": "XAUUSD",
        "risk_currency": "USD",
        "policy_id": "personal-policy",
        "policy_version": "v1",
    }
    values.update(changes)
    return PersonalTrustScope(**values)


def _ceremony(**changes: object) -> PersonalTrustBootstrapCeremony:
    descriptor = changes.pop("descriptor", _descriptor())
    values: dict[str, object] = {
        "ceremony_id": "ceremony-personal-000000000000000001",
        "descriptor": descriptor,
        "expected_public_key_fingerprint": descriptor.public_key_fingerprint,
        "scope": _scope(),
        "expected_authority_id": "synthetic-personal-key-authority",
        "trust_anchor_id": "personal-trust-anchor",
        "trust_anchor_version": "v1",
        "trust_anchor_fingerprint": "a" * 64,
        "presented_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(changes)
    return PersonalTrustBootstrapCeremony(**values)


def _decision(ceremony: PersonalTrustBootstrapCeremony, **changes: object) -> PersonalTrustDecision:
    values: dict[str, object] = {
        "decision_id": "decision-personal-000000000000000001",
        "ceremony_fingerprint": ceremony.fingerprint,
        "key_id": ceremony.descriptor.key_id,
        "public_key_fingerprint": ceremony.expected_public_key_fingerprint,
        "scope_fingerprint": ceremony.scope.fingerprint,
        "authority_id": ceremony.expected_authority_id,
        "trust_anchor_id": ceremony.trust_anchor_id,
        "trust_anchor_version": ceremony.trust_anchor_version,
        "trust_anchor_fingerprint": ceremony.trust_anchor_fingerprint,
        "ceremony_presented_at": ceremony.presented_at,
        "ceremony_expires_at": ceremony.expires_at,
        "status": PersonalTrustDecisionStatus.CONFIRMED,
        "verification_method": PersonalTrustVerificationMethod.OUT_OF_BAND_FINGERPRINT,
        "decided_at": NOW,
        "decision_reference": "personal-owner-fingerprint-check-1",
    }
    values.update(changes)
    return PersonalTrustDecision(**values)


def _local_recovery(**changes: object) -> PersonalKeyRecoveryStatus:
    values: dict[str, object] = {
        "key_id": "personal-key-1",
        "metadata_available": True,
        "signing_capability_available": True,
        "requires_external_recovery": False,
    }
    values.update(changes)
    return PersonalKeyRecoveryStatus(**values)


def _authority_request(ceremony: PersonalTrustBootstrapCeremony, **changes: object):
    values: dict[str, object] = {
        "authority_id": "synthetic-personal-key-authority",
        "trust_anchor_id": ceremony.trust_anchor_id,
        "trust_anchor_version": ceremony.trust_anchor_version,
        "trust_anchor_fingerprint": ceremony.trust_anchor_fingerprint,
        "key_id": ceremony.descriptor.key_id,
        "public_key_fingerprint": ceremony.expected_public_key_fingerprint,
        "key_generation": ceremony.descriptor.generation,
        "scope": ceremony.scope,
        "scope_fingerprint": ceremony.scope.fingerprint,
        "challenge_id": "challenge-personal-key-000000000000001",
        "requested_at": NOW - timedelta(seconds=2),
        "max_age_seconds": 30,
    }
    values.update(changes)
    return PersonalKeyAuthorityRequest(**values)


def _observation(request: PersonalKeyAuthorityRequest, **changes: object):
    values: dict[str, object] = {
        "availability": ExternalAuthorityAvailability.AVAILABLE,
        "provider_id": "synthetic-personal-key-provider",
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "authority_id": request.authority_id,
        "trust_anchor_id": request.trust_anchor_id,
        "trust_anchor_version": request.trust_anchor_version,
        "trust_anchor_fingerprint": request.trust_anchor_fingerprint,
        "key_id": request.key_id,
        "public_key_fingerprint": request.public_key_fingerprint,
        "key_generation": request.key_generation,
        "scope": request.scope,
        "scope_fingerprint": request.scope_fingerprint,
        "key_state": PersonalKeyAuthorityState.ACTIVE,
        "source_event_id": "synthetic-personal-key-event-1",
        "source_sequence": 1,
        "observed_at": NOW - timedelta(seconds=1),
        "retrieved_at": NOW,
        "expires_at": NOW + timedelta(seconds=30),
        "audit_reference": "synthetic-personal-key-audit-1",
        "source_authenticated": True,
        "trust_anchor_validated": True,
    }
    values.update(changes)
    return PersonalKeyAuthorityObservation(**values)


class _Provider:
    provider_id = "SYNTHETIC-PERSONAL-KEY-PROVIDER"

    def __init__(self, observation: PersonalKeyAuthorityObservation) -> None:
        self.observation = observation

    def observe(self, request: PersonalKeyAuthorityRequest) -> PersonalKeyAuthorityObservation:
        del request
        return self.observation


class _ReplayGuard:
    """Process-local test double; never used as production authority."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str, str], tuple[int, str, str]] = {}

    def check_and_record(self, **values: object) -> PersonalAuthorityReplayResult:
        key = (
            str(values["provider_id"]),
            str(values["authority_id"]),
            str(values["key_id"]),
        )
        sequence = int(values["source_sequence"])
        event_id = str(values["source_event_id"])
        fingerprint = str(values["observation_fingerprint"])
        existing = self._seen.get(key)
        status = PersonalAuthorityReplayStatus.ACCEPTED
        codes: tuple[str, ...] = ()
        if existing is not None:
            old_sequence, old_event, old_fingerprint = existing
            if sequence < old_sequence:
                status, codes = PersonalAuthorityReplayStatus.ROLLBACK, (
                    "PERSONAL_KEY_AUTHORITY_SEQUENCE_ROLLBACK",
                )
            elif sequence == old_sequence and (event_id, fingerprint) == (
                old_event,
                old_fingerprint,
            ):
                status, codes = PersonalAuthorityReplayStatus.DUPLICATE, (
                    "PERSONAL_KEY_AUTHORITY_DUPLICATE",
                )
            elif sequence == old_sequence:
                status, codes = PersonalAuthorityReplayStatus.CONFLICT, (
                    "PERSONAL_KEY_AUTHORITY_CONFLICT",
                )
        if status is PersonalAuthorityReplayStatus.ACCEPTED:
            self._seen[key] = (sequence, event_id, fingerprint)
        return PersonalAuthorityReplayResult(
            status=status,
            provider_id=key[0],
            authority_id=key[1],
            key_id=key[2],
            source_event_id=event_id,
            source_sequence=sequence,
            observation_fingerprint=fingerprint,
            checked_at=values["checked_at"],
            reason_codes=codes,
        )


class _MismatchedReplayGuard(_ReplayGuard):
    def check_and_record(self, **values: object) -> PersonalAuthorityReplayResult:
        result = super().check_and_record(**values)
        return result.model_copy(update={"observation_fingerprint": "f" * 64})


def _authority_assessment(
    *,
    ceremony: PersonalTrustBootstrapCeremony | None = None,
    request: PersonalKeyAuthorityRequest | None = None,
    **observation_changes: object,
):
    ceremony = ceremony or _ceremony()
    request = request or _authority_request(ceremony)
    observation = _observation(request, **observation_changes)
    return PersonalKeyAuthorityIntegration(_Provider(observation), _ReplayGuard()).assess(
        request=request,
        evaluated_at=NOW,
    )


def _recovery_authority_context(
    ceremony: PersonalTrustBootstrapCeremony,
    request: PersonalKeyAuthorityRequest,
) -> dict[str, object]:
    return {
        "authority_request": request,
        "descriptor": ceremony.descriptor,
        "scope": ceremony.scope,
        "expected_authority_id": ceremony.expected_authority_id,
        "expected_trust_anchor_id": ceremony.trust_anchor_id,
        "expected_trust_anchor_version": ceremony.trust_anchor_version,
        "expected_trust_anchor_fingerprint": ceremony.trust_anchor_fingerprint,
    }


def test_first_use_bootstrap_remains_pending_without_human_or_authority() -> None:
    ceremony = _ceremony()
    result = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=None,
        local_recovery=_local_recovery(),
        authority=None,
        evaluated_at=NOW,
    )
    assert result.status is PersonalTrustBootstrapStatus.PENDING_HUMAN_CONFIRMATION
    assert "HUMAN_FINGERPRINT_VERIFICATION_PENDING" in result.reason_codes
    assert "AUTHORITATIVE_PERSONAL_KEY_STATUS_UNAVAILABLE" in result.reason_codes
    assert result.independent_trust_established is False
    assert result.production_authorization_available is False
    assert result.execution_authority is False


def test_ceremony_rejects_fingerprint_mismatch_and_scope_is_bound() -> None:
    with pytest.raises(ValidationError, match="exact public-key fingerprint"):
        _ceremony(expected_public_key_fingerprint="b" * 64)
    first = _ceremony()
    second = _ceremony(scope=_scope(account_id="other-account"))
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize(
    "missing",
    ("expected_authority_id", "trust_anchor_id", "trust_anchor_version", "trust_anchor_fingerprint"),
)
def test_ceremony_requires_complete_trust_anchor_lineage(missing: str) -> None:
    values = _ceremony().model_dump(mode="python", exclude={"fingerprint", missing})
    with pytest.raises(ValidationError):
        PersonalTrustBootstrapCeremony(**values)


def test_ceremony_fingerprint_binds_trust_anchor_identity_version_and_fingerprint() -> None:
    ceremony = _ceremony()
    assert ceremony.fingerprint != _ceremony(trust_anchor_id="other-anchor").fingerprint
    assert ceremony.fingerprint != _ceremony(trust_anchor_version="v2").fingerprint
    assert ceremony.fingerprint != _ceremony(trust_anchor_fingerprint="b" * 64).fingerprint


@pytest.mark.parametrize(
    "method",
    (
        PersonalTrustVerificationMethod.LOCAL_KEY_SIGNATURE,
        PersonalTrustVerificationMethod.LOCAL_SELF_AUTHORED_MANIFEST,
    ),
)
def test_key_or_local_manifest_cannot_authorize_itself(method) -> None:
    with pytest.raises(ValidationError, match="out-of-band"):
        _decision(_ceremony(), verification_method=method)


def test_exact_local_confirmation_is_recorded_but_never_independent() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    authority = _authority_assessment(ceremony=ceremony, request=request)
    result = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=authority,
        authority_request=request,
        evaluated_at=NOW,
    )
    assert result.status is PersonalTrustBootstrapStatus.LOCAL_CONFIRMATION_RECORDED
    assert result.local_confirmation_recorded is True
    assert result.local_key_available is True
    assert "INDEPENDENT_PERSONAL_KEY_AUTHORITY_NOT_ESTABLISHED" in result.reason_codes
    assert result.independent_trust_established is False
    assert result.authoritative_revocation_established is False
    assert result.replay_rollback_protection_established is False


def test_scope_substitution_in_decision_fails_closed() -> None:
    ceremony = _ceremony()
    result = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony, scope_fingerprint="b" * 64),
        local_recovery=_local_recovery(),
        authority=None,
        evaluated_at=NOW,
    )
    assert result.status is PersonalTrustBootstrapStatus.INVALID
    assert "PERSONAL_TRUST_DECISION_BINDING_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"authority_id": "other-authority"}, "PERSONAL_TRUST_DECISION_BINDING_MISMATCH"),
        ({"trust_anchor_id": "other-anchor"}, "PERSONAL_TRUST_DECISION_BINDING_MISMATCH"),
        ({"trust_anchor_version": "v2"}, "PERSONAL_TRUST_DECISION_BINDING_MISMATCH"),
        ({"trust_anchor_fingerprint": "b" * 64}, "PERSONAL_TRUST_DECISION_BINDING_MISMATCH"),
    ),
)
def test_trust_anchor_substitution_in_decision_fails_closed(
    changes: dict[str, object], expected: str
) -> None:
    ceremony = _ceremony()
    result = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony, **changes),
        local_recovery=_local_recovery(),
        authority=None,
        evaluated_at=NOW,
    )
    assert result.status is PersonalTrustBootstrapStatus.INVALID
    assert expected in result.reason_codes


def test_confirmation_cannot_be_replayed_across_trust_anchor_versions() -> None:
    original = _ceremony(trust_anchor_version="v1")
    replacement = _ceremony(trust_anchor_version="v2")
    result = PersonalTrustBootstrapService().assess(
        ceremony=replacement,
        decision=_decision(original),
        local_recovery=_local_recovery(),
        authority=None,
        evaluated_at=NOW,
    )
    assert result.status is PersonalTrustBootstrapStatus.INVALID
    assert "PERSONAL_TRUST_DECISION_BINDING_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    "missing",
    ("authority_id", "trust_anchor_id", "trust_anchor_version", "trust_anchor_fingerprint"),
)
def test_decision_requires_complete_trust_anchor_lineage(missing: str) -> None:
    ceremony = _ceremony()
    values = _decision(ceremony).model_dump(mode="python", exclude={"fingerprint", missing})
    with pytest.raises(ValidationError):
        PersonalTrustDecision(**values)


@pytest.mark.parametrize(
    "decided_at",
    (
        NOW - timedelta(minutes=2),
        NOW + timedelta(minutes=10),
        NOW + timedelta(minutes=11),
    ),
)
def test_decision_outside_ceremony_window_is_rejected_at_construction(
    decided_at: datetime,
) -> None:
    with pytest.raises(ValidationError, match="ceremony window"):
        _decision(_ceremony(), decided_at=decided_at)


def test_decision_window_boundaries_and_future_evaluation_are_deterministic() -> None:
    ceremony = _ceremony()
    at_start = _decision(ceremony, decided_at=ceremony.presented_at)
    accepted = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=at_start,
        local_recovery=_local_recovery(),
        authority=None,
        evaluated_at=NOW,
    )
    assert accepted.status is PersonalTrustBootstrapStatus.LOCAL_CONFIRMATION_RECORDED

    future = _decision(ceremony, decided_at=NOW + timedelta(minutes=1))
    rejected = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=future,
        local_recovery=_local_recovery(),
        authority=None,
        evaluated_at=NOW,
    )
    assert rejected.status is PersonalTrustBootstrapStatus.INVALID
    assert "PERSONAL_TRUST_DECISION_TIME_INVALID" in rejected.reason_codes


def test_decision_rejects_malformed_or_inconsistent_ceremony_time() -> None:
    ceremony = _ceremony()
    with pytest.raises(ValidationError, match="timezone awareness"):
        _decision(ceremony, decided_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="ceremony window"):
        _decision(
            ceremony,
            ceremony_presented_at=ceremony.expires_at,
            ceremony_expires_at=ceremony.presented_at,
        )


def test_default_personal_key_authority_is_unavailable() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    result = PersonalKeyAuthorityIntegration().assess(request=request, evaluated_at=NOW)
    assert result.status is PersonalKeyAuthorityAssessmentStatus.UNAVAILABLE
    assert result.independently_authoritative is False
    assert result.production_authorization_available is False


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"public_key_fingerprint": "b" * 64}, "PERSONAL_KEY_FINGERPRINT_MISMATCH"),
        (
            {
                "scope": _scope(account_id="other-account"),
                "scope_fingerprint": _scope(account_id="other-account").fingerprint,
            },
            "PERSONAL_KEY_SCOPE_MISMATCH",
        ),
        ({"source_authenticated": False}, "PERSONAL_KEY_AUTHORITY_UNAUTHENTICATED"),
        ({"trust_anchor_validated": False}, "PERSONAL_KEY_TRUST_ANCHOR_UNVERIFIED"),
        (
            {"observed_at": NOW - timedelta(minutes=5)},
            "PERSONAL_KEY_AUTHORITY_OBSERVATION_STALE",
        ),
    ),
)
def test_key_authority_rejects_mismatch_unauthenticated_and_stale_claims(
    changes: dict[str, object], code: str
) -> None:
    result = _authority_assessment(**changes)
    assert result.status in {
        PersonalKeyAuthorityAssessmentStatus.REJECTED,
        PersonalKeyAuthorityAssessmentStatus.STALE,
    }
    assert code in result.reason_codes
    assert result.independently_authoritative is False


@pytest.mark.parametrize(
    ("state", "extra", "expected"),
    (
        (
            PersonalKeyAuthorityState.REVOKED,
            {},
            PersonalKeyAuthorityAssessmentStatus.REVOKED,
        ),
        (
            PersonalKeyAuthorityState.SUPERSEDED,
            {
                "replacement_key_id": "personal-key-2",
                "replacement_key_fingerprint": "b" * 64,
                "replacement_generation": 2,
            },
            PersonalKeyAuthorityAssessmentStatus.SUPERSEDED,
        ),
        (
            PersonalKeyAuthorityState.COMPROMISED,
            {},
            PersonalKeyAuthorityAssessmentStatus.COMPROMISED,
        ),
    ),
)
def test_terminal_authority_states_remain_distinct_and_non_authorizing(
    state: PersonalKeyAuthorityState,
    extra: dict[str, object],
    expected: PersonalKeyAuthorityAssessmentStatus,
) -> None:
    result = _authority_assessment(key_state=state, **extra)
    assert result.status is expected
    assert result.execution_authority is False


def test_provider_conflict_and_unavailability_fail_closed() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    conflict = PersonalKeyAuthorityObservation(
        availability=ExternalAuthorityAvailability.CONFLICT,
        provider_id="synthetic-personal-key-provider",
        request_fingerprint=request.request_fingerprint,
        challenge_id=request.challenge_id,
        retrieved_at=NOW,
        reason_codes=("PERSONAL_KEY_AUTHORITY_CONFLICT",),
    )
    result = PersonalKeyAuthorityIntegration(_Provider(conflict), _ReplayGuard()).assess(
        request=request,
        evaluated_at=NOW,
    )
    assert result.status is PersonalKeyAuthorityAssessmentStatus.CONFLICT


def test_sequence_rollback_and_duplicate_observation_are_rejected() -> None:
    ceremony = _ceremony()
    rollback_request = _authority_request(ceremony, minimum_source_sequence=1)
    rollback = PersonalKeyAuthorityIntegration(
        _Provider(_observation(rollback_request, source_sequence=1)), _ReplayGuard()
    ).assess(request=rollback_request, evaluated_at=NOW)
    assert rollback.status is PersonalKeyAuthorityAssessmentStatus.REPLAYED

    request = _authority_request(ceremony)
    guard = _ReplayGuard()
    integration = PersonalKeyAuthorityIntegration(_Provider(_observation(request)), guard)
    first = integration.assess(request=request, evaluated_at=NOW)
    second = integration.assess(request=request, evaluated_at=NOW)
    assert first.status is PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED
    assert second.status is PersonalKeyAuthorityAssessmentStatus.REPLAYED


def test_replay_protector_result_must_bind_the_exact_observation() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    result = PersonalKeyAuthorityIntegration(
        _Provider(_observation(request)), _MismatchedReplayGuard()
    ).assess(request=request, evaluated_at=NOW)
    assert result.status is PersonalKeyAuthorityAssessmentStatus.REJECTED
    assert result.reason_codes == ("PERSONAL_KEY_REPLAY_RESULT_MISMATCH",)


@pytest.mark.parametrize(
    ("request_changes", "expected_code"),
    (
        ({"key_id": "other-key"}, "PERSONAL_KEY_REQUEST_KEY_MISMATCH"),
        ({"key_generation": 2}, "PERSONAL_KEY_REQUEST_GENERATION_MISMATCH"),
        (
            {
                "scope": _scope(account_id="other-account"),
                "scope_fingerprint": _scope(account_id="other-account").fingerprint,
            },
            "PERSONAL_KEY_REQUEST_SCOPE_MISMATCH",
        ),
        ({"authority_id": "other-authority"}, "PERSONAL_KEY_REQUEST_AUTHORITY_MISMATCH"),
        ({"trust_anchor_id": "other-anchor"}, "PERSONAL_KEY_REQUEST_TRUST_ANCHOR_ID_MISMATCH"),
        (
            {"trust_anchor_version": "v2"},
            "PERSONAL_KEY_REQUEST_TRUST_ANCHOR_VERSION_MISMATCH",
        ),
        (
            {"trust_anchor_fingerprint": "b" * 64},
            "PERSONAL_KEY_REQUEST_TRUST_ANCHOR_FINGERPRINT_MISMATCH",
        ),
    ),
)
def test_cross_subject_authority_assessment_is_rejected_by_bootstrap_and_recovery(
    request_changes: dict[str, object], expected_code: str
) -> None:
    ceremony = _ceremony()
    substituted_request = _authority_request(ceremony, **request_changes)
    authority = _authority_assessment(ceremony=ceremony, request=substituted_request)

    bootstrap = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=authority,
        authority_request=substituted_request,
        evaluated_at=NOW,
    )
    assert bootstrap.authority_assessment_status is PersonalKeyAuthorityAssessmentStatus.REJECTED
    assert expected_code in bootstrap.reason_codes
    assert bootstrap.independent_trust_established is False

    recovery = PersonalTrustBootstrapService().assess_recovery(
        local_recovery=_local_recovery(),
        authority=authority,
        evaluated_at=NOW,
        **_recovery_authority_context(ceremony, substituted_request),
    )
    assert recovery.state is PersonalRecoveryState.AUTHORITY_UNAVAILABLE
    assert expected_code in recovery.reason_codes
    assert recovery.execution_authority is False


def test_authority_assessment_cannot_be_reused_with_a_different_request() -> None:
    ceremony = _ceremony()
    original = _authority_request(ceremony)
    authority = _authority_assessment(ceremony=ceremony, request=original)
    different = _authority_request(
        ceremony,
        challenge_id="challenge-personal-key-000000000000002",
    )
    bootstrap = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=authority,
        authority_request=different,
        evaluated_at=NOW,
    )
    assert bootstrap.authority_assessment_status is PersonalKeyAuthorityAssessmentStatus.REJECTED
    assert any("CHALLENGE_ID_MISMATCH" in code for code in bootstrap.reason_codes)

    recovery = PersonalTrustBootstrapService().assess_recovery(
        local_recovery=_local_recovery(),
        authority=authority,
        evaluated_at=NOW,
        **_recovery_authority_context(ceremony, different),
    )
    assert recovery.state is PersonalRecoveryState.AUTHORITY_UNAVAILABLE
    assert any("CHALLENGE_ID_MISMATCH" in code for code in recovery.reason_codes)


def test_authority_assessment_recomputes_and_validates_request_fingerprint() -> None:
    authority = _authority_assessment()
    values = authority.model_dump(mode="python")
    values["scope"] = authority.scope
    values["request_fingerprint"] = "b" * 64
    with pytest.raises(ValidationError, match="request lineage is inconsistent"):
        PersonalKeyAuthorityAssessment(**values)


def test_authority_assessment_freshness_is_revalidated_at_composition_time() -> None:
    ceremony = _ceremony(expires_at=NOW + timedelta(hours=1))
    request = _authority_request(ceremony)
    authority = _authority_assessment(ceremony=ceremony, request=request)
    evaluated_at = NOW + timedelta(seconds=31)
    result = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=authority,
        authority_request=request,
        evaluated_at=evaluated_at,
    )
    assert result.authority_assessment_status is PersonalKeyAuthorityAssessmentStatus.REJECTED
    assert "PERSONAL_KEY_AUTHORITY_ASSESSMENT_STALE" in result.reason_codes
    assert "PERSONAL_KEY_AUTHORITY_ASSESSMENT_EXPIRED" in result.reason_codes
    assert result.production_authorization_available is False


def test_composition_rejects_authority_retrieved_before_request() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    validated = _authority_assessment(ceremony=ceremony, request=request)
    values = validated.model_dump(mode="python", exclude_computed_fields=True)
    values.update(
        observed_at=request.requested_at - timedelta(seconds=2),
        retrieved_at=request.requested_at - timedelta(seconds=1),
    )
    reconstructed = PersonalKeyAuthorityAssessment(**values)

    bootstrap = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=reconstructed,
        authority_request=request,
        evaluated_at=NOW,
    )
    assert bootstrap.authority_assessment_status is PersonalKeyAuthorityAssessmentStatus.REJECTED
    assert "PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID" in bootstrap.reason_codes
    assert bootstrap.independent_trust_established is False
    assert bootstrap.production_authorization_available is False
    assert bootstrap.submission_allowed is False
    assert bootstrap.execution_authority is False

    recovery = PersonalTrustBootstrapService().assess_recovery(
        local_recovery=_local_recovery(),
        authority=reconstructed,
        evaluated_at=NOW,
        **_recovery_authority_context(ceremony, request),
    )
    assert recovery.state is PersonalRecoveryState.AUTHORITY_UNAVAILABLE
    assert recovery.authority_assessment_status is PersonalKeyAuthorityAssessmentStatus.REJECTED
    assert "PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID" in recovery.reason_codes
    assert recovery.production_authorization_available is False
    assert recovery.submission_allowed is False
    assert recovery.execution_authority is False


@pytest.mark.parametrize(
    "retrieved_at",
    (
        NOW - timedelta(seconds=2),
        NOW - timedelta(seconds=1),
    ),
    ids=("exactly-at-request", "after-request"),
)
def test_composition_accepts_observation_before_request_when_retrieval_is_not_before_request(
    retrieved_at: datetime,
) -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    assert request.requested_at == NOW - timedelta(seconds=2)
    validated = _authority_assessment(ceremony=ceremony, request=request)
    values = validated.model_dump(mode="python", exclude_computed_fields=True)
    values.update(
        observed_at=request.requested_at - timedelta(seconds=1),
        retrieved_at=retrieved_at,
    )
    reconstructed = PersonalKeyAuthorityAssessment(**values)

    bootstrap = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=reconstructed,
        authority_request=request,
        evaluated_at=NOW,
    )
    assert (
        bootstrap.authority_assessment_status
        is PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED
    )
    assert "PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID" not in bootstrap.reason_codes
    assert bootstrap.production_authorization_available is False
    assert bootstrap.submission_allowed is False
    assert bootstrap.execution_authority is False

    recovery = PersonalTrustBootstrapService().assess_recovery(
        local_recovery=_local_recovery(),
        authority=reconstructed,
        evaluated_at=NOW,
        **_recovery_authority_context(ceremony, request),
    )
    assert recovery.state is PersonalRecoveryState.LOCAL_KEY_AVAILABLE
    assert (
        recovery.authority_assessment_status
        is PersonalKeyAuthorityAssessmentStatus.STRUCTURALLY_VALIDATED
    )
    assert "PERSONAL_KEY_AUTHORITY_ASSESSMENT_TIME_INVALID" not in recovery.reason_codes
    assert recovery.production_authorization_available is False
    assert recovery.submission_allowed is False
    assert recovery.execution_authority is False


@pytest.mark.parametrize(
    ("local_changes", "authority", "flags", "expected"),
    (
        (
            {
                "metadata_available": False,
                "signing_capability_available": False,
                "requires_external_recovery": True,
                "reason_codes": ("DPAPI_KEY_NOT_FOUND",),
            },
            None,
            {},
            PersonalRecoveryState.KEY_LOST,
        ),
        (
            {
                "signing_capability_available": False,
                "requires_external_recovery": True,
                "reason_codes": ("DPAPI_KEY_RECOVERY_FAILED",),
            },
            None,
            {},
            PersonalRecoveryState.KEY_CORRUPT_OR_UNDECRYPTABLE,
        ),
        ({}, None, {"suspected_compromise": True}, PersonalRecoveryState.SUSPECTED_COMPROMISE),
        ({}, None, {"interrupted_operation": True}, PersonalRecoveryState.INTERRUPTED),
        ({}, None, {"rollback_detected": True}, PersonalRecoveryState.ROLLBACK_DETECTED),
    ),
)
def test_recovery_states_fail_closed_without_replacement_or_reactivation(
    local_changes: dict[str, object], authority, flags: dict[str, bool], expected
) -> None:
    result = PersonalTrustBootstrapService().assess_recovery(
        local_recovery=_local_recovery(**local_changes),
        authority=authority,
        evaluated_at=NOW,
        **flags,
    )
    assert result.state is expected
    assert result.automatic_key_replacement_allowed is False
    assert result.revoked_key_reactivation_allowed is False
    assert result.production_authorization_available is False


def test_revoked_and_superseded_keys_cannot_be_reactivated() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    for state, extra, expected in (
        (PersonalKeyAuthorityState.REVOKED, {}, PersonalRecoveryState.REVOKED),
        (
            PersonalKeyAuthorityState.SUPERSEDED,
            {
                "replacement_key_id": "personal-key-2",
                "replacement_key_fingerprint": "b" * 64,
                "replacement_generation": 2,
            },
            PersonalRecoveryState.SUPERSEDED,
        ),
    ):
        authority = _authority_assessment(
            ceremony=ceremony,
            request=request,
            key_state=state,
            **extra,
        )
        result = PersonalTrustBootstrapService().assess_recovery(
            local_recovery=_local_recovery(),
            authority=authority,
            evaluated_at=NOW,
            **_recovery_authority_context(ceremony, request),
        )
        assert result.state is expected
        assert result.revoked_key_reactivation_allowed is False


@pytest.mark.parametrize(
    ("authority_state", "expected"),
    (
        (PersonalKeyAuthorityState.COMPROMISED, PersonalRecoveryState.SUSPECTED_COMPROMISE),
        (
            PersonalKeyAuthorityState.RECOVERY_REQUIRED,
            PersonalRecoveryState.AUTHORITY_RECOVERY_REQUIRED,
        ),
    ),
)
def test_authority_compromise_and_recovery_states_never_fall_through_as_available(
    authority_state: PersonalKeyAuthorityState,
    expected: PersonalRecoveryState,
) -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    result = PersonalTrustBootstrapService().assess_recovery(
        local_recovery=_local_recovery(),
        authority=_authority_assessment(
            ceremony=ceremony,
            request=request,
            key_state=authority_state,
        ),
        evaluated_at=NOW,
        **_recovery_authority_context(ceremony, request),
    )
    assert result.state is expected
    assert result.automatic_key_replacement_allowed is False
    assert result.execution_authority is False


def test_local_bootstrap_cannot_satisfy_production_readiness() -> None:
    ceremony = _ceremony()
    request = _authority_request(ceremony)
    local = PersonalTrustBootstrapService().assess(
        ceremony=ceremony,
        decision=_decision(ceremony),
        local_recovery=_local_recovery(),
        authority=_authority_assessment(ceremony=ceremony, request=request),
        authority_request=request,
        evaluated_at=NOW,
    )
    report = ProductionAuthorizationReadinessGate().evaluate(now=NOW, personal_trust=local)
    checks = {item.prerequisite: item for item in report.checks}
    assert checks[ProductionReadinessPrerequisite.PERSONAL_TRUST_BOOTSTRAP].status is ProductionReadinessCheckStatus.BLOCKED
    assert checks[ProductionReadinessPrerequisite.PERSONAL_KEY_STATUS_AUTHORITY].status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert checks[ProductionReadinessPrerequisite.PERSONAL_REPLAY_ROLLBACK_PROTECTION].status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_bootstrap_service_has_no_execution_or_provider_reachability() -> None:
    source = inspect.getsource(bootstrap_module)
    for forbidden in (
        "MetaTrader5",
        "order_send",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "ExecutionStateStore",
        ".submit(",
    ):
        assert forbidden not in source
