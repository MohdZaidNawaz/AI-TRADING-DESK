"""Cryptographic verification tests using isolated, non-authoritative keys."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.models.independent_trust_verification import (
    ConfiguredEd25519TrustAnchor,
    IndependentKeyStatusStatement,
    IndependentReplayResult,
    IndependentReplayStatus,
    IndependentTrustVerificationStatus,
    SignedIndependentKeyStatusStatement,
    canonical_independent_key_status_payload,
)
from app.models.personal_trust_bootstrap import (
    PersonalKeyAuthorityRequest,
    PersonalKeyAuthorityState,
    PersonalTrustScope,
)
from app.services.independent_trust_verification import IndependentKeyStatusVerifier
from app.services.durable_independent_replay import DurableIndependentReplayStore


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class _AcceptedReplay:
    def check_and_record(self, *, binding, checked_at):
        return IndependentReplayResult(
            status=IndependentReplayStatus.ACCEPTED,
            binding_fingerprint=binding.binding_fingerprint,
            checked_at=checked_at,
        )


class _ReplayOutcome:
    def __init__(self, status: IndependentReplayStatus) -> None:
        self.status = status

    def check_and_record(self, *, binding, checked_at):
        return IndependentReplayResult(
            status=self.status,
            binding_fingerprint=binding.binding_fingerprint,
            checked_at=checked_at,
            reason_codes=(f"TEST_REPLAY_{self.status.value.upper()}",),
        )


def _key_material():
    private = Ed25519PrivateKey.generate()
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private, pem


def _scope(**changes: object) -> PersonalTrustScope:
    values: dict[str, object] = {
        "scope_id": "personal-xauusd-disabled",
        "account_id": "demo-123",
        "execution_mode": "disabled",
        "canonical_symbol": "XAUUSD",
        "risk_currency": "USD",
        "policy_id": "personal-policy",
        "policy_version": "v1",
    }
    values.update(changes)
    return PersonalTrustScope(**values)


def _anchor(pem: str, **changes: object) -> ConfiguredEd25519TrustAnchor:
    values: dict[str, object] = {
        "authority_id": "external-key-authority",
        "anchor_id": "external-root-1",
        "anchor_version": "v1",
        "public_key_pem": pem,
        "valid_from": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=1),
        "provenance_reference": "owner-reviewed-offline-anchor-record",
    }
    values.update(changes)
    return ConfiguredEd25519TrustAnchor(**values)


def _request(anchor: ConfiguredEd25519TrustAnchor, **changes: object) -> PersonalKeyAuthorityRequest:
    scope = changes.pop("scope", _scope())
    values: dict[str, object] = {
        "authority_id": anchor.authority_id,
        "trust_anchor_id": anchor.anchor_id,
        "trust_anchor_version": anchor.anchor_version,
        "trust_anchor_fingerprint": anchor.public_key_fingerprint,
        "key_id": "personal-key-1",
        "public_key_fingerprint": "a" * 64,
        "key_generation": 1,
        "scope": scope,
        "scope_fingerprint": scope.fingerprint,
        "challenge_id": "challenge-independent-authority-000000001",
        "requested_at": NOW - timedelta(seconds=2),
        "max_age_seconds": 30,
    }
    values.update(changes)
    return PersonalKeyAuthorityRequest(**values)


def _statement(
    request: PersonalKeyAuthorityRequest,
    anchor: ConfiguredEd25519TrustAnchor,
    **changes: object,
) -> IndependentKeyStatusStatement:
    values: dict[str, object] = {
        "statement_id": "authority-statement-000000000000000001",
        "authority_id": request.authority_id,
        "anchor_id": anchor.anchor_id,
        "anchor_version": anchor.anchor_version,
        "anchor_fingerprint": anchor.public_key_fingerprint,
        "request_fingerprint": request.request_fingerprint,
        "challenge_id": request.challenge_id,
        "key_id": request.key_id,
        "public_key_fingerprint": request.public_key_fingerprint,
        "key_generation": request.key_generation,
        "scope": request.scope,
        "scope_fingerprint": request.scope_fingerprint,
        "key_state": PersonalKeyAuthorityState.ACTIVE,
        "source_sequence": 4,
        "observed_at": NOW - timedelta(seconds=1),
        "issued_at": NOW - timedelta(milliseconds=500),
        "expires_at": NOW + timedelta(seconds=30),
    }
    values.update(changes)
    return IndependentKeyStatusStatement(**values)


def _envelope(statement: IndependentKeyStatusStatement, private: Ed25519PrivateKey):
    signature = private.sign(canonical_independent_key_status_payload(statement))
    return SignedIndependentKeyStatusStatement(
        statement=statement,
        signature_base64=base64.b64encode(signature).decode("ascii"),
    )


def _verified():
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor)
    statement = _statement(request, anchor)
    result = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request,
        envelope=_envelope(statement, private),
        evaluated_at=NOW,
    )
    return result, private, anchor, request, statement


def test_valid_signature_and_exact_lineage_are_cryptographically_verified_but_non_authorizing() -> None:
    result, *_ = _verified()
    assert result.status is IndependentTrustVerificationStatus.CRYPTOGRAPHICALLY_VERIFIED
    assert result.signature_verified is True
    assert result.replay_binding_verified is True
    assert result.independently_authoritative is False
    assert result.production_authorization_available is False
    assert result.submission_allowed is False
    assert result.execution_authority is False


def test_missing_anchor_and_missing_external_replay_protection_fail_closed() -> None:
    result, private, anchor, request, statement = _verified()
    assert result.signature_verified is True
    missing = IndependentKeyStatusVerifier().verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert missing.status is IndependentTrustVerificationStatus.UNAVAILABLE
    assert "INDEPENDENT_TRUST_ANCHOR_UNAVAILABLE" in missing.reason_codes

    no_replay = IndependentKeyStatusVerifier(anchor).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert no_replay.status is IndependentTrustVerificationStatus.UNAVAILABLE
    assert no_replay.signature_verified is True
    assert no_replay.replay_binding_verified is False
    assert "EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE" in no_replay.reason_codes


def test_invalid_signature_and_authority_supplied_key_substitution_are_rejected() -> None:
    _result, private, anchor, request, statement = _verified()
    other_private, other_pem = _key_material()
    invalid = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request,
        envelope=_envelope(statement, other_private),
        evaluated_at=NOW,
    )
    assert invalid.status is IndependentTrustVerificationStatus.REJECTED
    assert invalid.signature_verified is False
    assert invalid.reason_codes == ("AUTHORITY_STATEMENT_SIGNATURE_INVALID",)

    wrong_anchor = _anchor(other_pem)
    substituted = IndependentKeyStatusVerifier(wrong_anchor, _AcceptedReplay()).verify(
        request=request,
        envelope=_envelope(statement, private),
        evaluated_at=NOW,
    )
    assert substituted.status is IndependentTrustVerificationStatus.REJECTED
    assert "TRUST_ANCHOR_FINGERPRINT_MISMATCH" in substituted.reason_codes


@pytest.mark.parametrize(
    ("request_change", "statement_change", "code"),
    [
        ({"authority_id": "other-authority"}, {}, "TRUST_ANCHOR_AUTHORITY_MISMATCH"),
        ({"key_id": "other-key"}, {}, "AUTHORITY_STATEMENT_KEY_MISMATCH"),
        ({"key_generation": 2}, {}, "AUTHORITY_STATEMENT_GENERATION_MISMATCH"),
        ({"scope": _scope(account_id="other-account")}, {}, "AUTHORITY_STATEMENT_SCOPE_CONTENT_MISMATCH"),
        ({}, {"challenge_id": "challenge-independent-authority-000000099"}, "AUTHORITY_STATEMENT_CHALLENGE_MISMATCH"),
    ],
)
def test_cross_authority_key_generation_scope_and_challenge_reuse_is_rejected(
    request_change: dict[str, object], statement_change: dict[str, object], code: str
) -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    original_request = _request(anchor)
    statement = _statement(original_request, anchor, **statement_change)
    request = _request(anchor, **request_change)
    result = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert result.status is IndependentTrustVerificationStatus.REJECTED
    assert code in result.reason_codes


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        (
            {"observed_at": NOW - timedelta(minutes=2), "issued_at": NOW - timedelta(seconds=1)},
            "AUTHORITY_STATEMENT_STALE",
        ),
        (
            {"observed_at": NOW + timedelta(seconds=1), "issued_at": NOW + timedelta(seconds=1)},
            "AUTHORITY_STATEMENT_FROM_FUTURE",
        ),
        (
            {"observed_at": NOW - timedelta(seconds=5), "issued_at": NOW - timedelta(seconds=4), "expires_at": NOW},
            "AUTHORITY_STATEMENT_EXPIRED",
        ),
        (
            {"observed_at": NOW - timedelta(seconds=3), "issued_at": NOW - timedelta(seconds=3)},
            "AUTHORITY_STATEMENT_PREDATES_REQUEST",
        ),
    ],
)
def test_stale_expired_future_and_predating_statements_fail_closed(
    changes: dict[str, object], code: str
) -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor)
    statement = _statement(request, anchor, **changes)
    result = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert result.status in {
        IndependentTrustVerificationStatus.STALE,
        IndependentTrustVerificationStatus.REJECTED,
    }
    assert code in result.reason_codes


@pytest.mark.parametrize(
    ("replay_status", "expected"),
    [
        (IndependentReplayStatus.DUPLICATE, IndependentTrustVerificationStatus.REPLAYED),
        (IndependentReplayStatus.ROLLBACK, IndependentTrustVerificationStatus.ROLLBACK),
        (IndependentReplayStatus.CONFLICT, IndependentTrustVerificationStatus.CONFLICT),
        (IndependentReplayStatus.UNAVAILABLE, IndependentTrustVerificationStatus.UNAVAILABLE),
    ],
)
def test_replayed_rollback_conflicting_and_unavailable_sequences_fail_closed(
    replay_status: IndependentReplayStatus,
    expected: IndependentTrustVerificationStatus,
) -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor)
    statement = _statement(request, anchor)
    result = IndependentKeyStatusVerifier(anchor, _ReplayOutcome(replay_status)).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert result.status is expected
    assert result.execution_authority is False


def test_request_minimum_sequence_and_anchor_validity_prevent_rollback() -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor, minimum_source_sequence=4)
    statement = _statement(request, anchor, source_sequence=4)
    rollback = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert rollback.status is IndependentTrustVerificationStatus.ROLLBACK
    assert "AUTHORITY_STATEMENT_SEQUENCE_ROLLBACK" in rollback.reason_codes

    later_anchor = _anchor(pem, valid_from=NOW)
    earlier_request = _request(later_anchor, requested_at=NOW - timedelta(seconds=2))
    predating = _statement(
        earlier_request,
        later_anchor,
        observed_at=NOW - timedelta(seconds=1),
        issued_at=NOW - timedelta(milliseconds=500),
    )
    result = IndependentKeyStatusVerifier(later_anchor, _AcceptedReplay()).verify(
        request=earlier_request,
        envelope=_envelope(predating, private),
        evaluated_at=NOW,
    )
    assert result.status is IndependentTrustVerificationStatus.REJECTED
    assert "AUTHORITY_STATEMENT_PREDATES_ANCHOR" in result.reason_codes


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PersonalKeyAuthorityState.REVOKED, IndependentTrustVerificationStatus.REVOKED),
        (PersonalKeyAuthorityState.COMPROMISED, IndependentTrustVerificationStatus.COMPROMISED),
        (PersonalKeyAuthorityState.RECOVERY_REQUIRED, IndependentTrustVerificationStatus.RECOVERY_REQUIRED),
    ],
)
def test_authenticated_terminal_states_remain_fail_closed(
    state: PersonalKeyAuthorityState,
    expected: IndependentTrustVerificationStatus,
) -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor)
    statement = _statement(request, anchor, key_state=state)
    result = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert result.status is expected
    assert result.signature_verified is True
    assert result.production_authorization_available is False


def test_supersession_requires_and_preserves_replacement_lineage() -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor)
    with pytest.raises(ValidationError, match="replacement lineage"):
        _statement(request, anchor, key_state=PersonalKeyAuthorityState.SUPERSEDED)
    statement = _statement(
        request,
        anchor,
        key_state=PersonalKeyAuthorityState.SUPERSEDED,
        replacement_key_id="personal-key-2",
        replacement_key_fingerprint="b" * 64,
        replacement_generation=2,
    )
    result = IndependentKeyStatusVerifier(anchor, _AcceptedReplay()).verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    assert result.status is IndependentTrustVerificationStatus.SUPERSEDED


def test_models_are_immutable_and_authorization_flags_cannot_be_true() -> None:
    result, *_ = _verified()
    with pytest.raises(ValidationError):
        result.execution_authority = True  # type: ignore[misc]
    payload = result.model_dump(exclude={"fingerprint"})
    payload["production_authorization_available"] = True
    with pytest.raises(ValidationError):
        type(result).model_validate(payload)


def test_model_shaped_inputs_cannot_claim_independent_trust_or_external_durability() -> None:
    _result, private, anchor, request, statement = _verified()

    anchor_payload = anchor.model_dump(exclude={"public_key_fingerprint"})
    anchor_payload["independently_trusted"] = True
    with pytest.raises(ValidationError):
        ConfiguredEd25519TrustAnchor.model_validate(anchor_payload)

    with pytest.raises(ValidationError):
        IndependentReplayResult(
            status=IndependentReplayStatus.ACCEPTED,
            binding_fingerprint="a" * 64,
            checked_at=NOW,
            externally_durable=True,
        )

    envelope_payload = _envelope(statement, private).model_dump(
        exclude={"envelope_fingerprint"}
    )
    envelope_payload["public_key_pem"] = anchor.public_key_pem
    with pytest.raises(ValidationError):
        SignedIndependentKeyStatusStatement.model_validate(envelope_payload)


def test_durable_local_replay_integrates_without_claiming_external_authority(tmp_path) -> None:
    private, pem = _key_material()
    anchor = _anchor(pem)
    request = _request(anchor)
    statement = _statement(request, anchor)
    store = DurableIndependentReplayStore.initialize(tmp_path / "replay.sqlite3", now=NOW)
    verifier = IndependentKeyStatusVerifier(anchor, store)

    first = verifier.verify(
        request=request, envelope=_envelope(statement, private), evaluated_at=NOW
    )
    replayed = IndependentKeyStatusVerifier(
        anchor, DurableIndependentReplayStore(tmp_path / "replay.sqlite3")
    ).verify(request=request, envelope=_envelope(statement, private), evaluated_at=NOW)

    assert first.status is IndependentTrustVerificationStatus.CRYPTOGRAPHICALLY_VERIFIED
    assert first.replay_locally_durable is True
    assert first.replay_atomic_consumption is True
    assert first.external_rollback_resistance is False
    assert first.independently_authoritative is False
    assert first.production_authorization_available is False
    assert replayed.status is IndependentTrustVerificationStatus.REPLAYED
