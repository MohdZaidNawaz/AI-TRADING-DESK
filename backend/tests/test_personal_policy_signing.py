"""Phase 13I personal signing tests. Keys are temporary and test-only."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from app.models.demo_execution import ControlledDemoExecutionPolicy, DemoInstrumentBinding
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
from app.models.portfolio_risk import SourceAccountKey
from app.services import personal_policy_signing as signing_module
from app.services.personal_policy_signing import (
    PersonalKeyProviderUnavailable,
    PersonalPolicySigningService,
    PersonalPolicyVerificationService,
    UnavailableWindowsProtectedKeyProvider,
)


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _policy(**changes: object) -> ControlledDemoExecutionPolicy:
    values: dict[str, object] = {
        "policy_id": "personal-policy",
        "policy_version": "v1",
        "allowed_account": SourceAccountKey(
            source_id="mt5", source_account_id="demo-123", account_reference="Demo-Server"
        ),
        "instrument": DemoInstrumentBinding(canonical_symbol="XAUUSD", broker_symbol="XAUUSD"),
        "risk_currency": "USD",
        "allowed_order_types": ("MARKET",),
        "allowed_filling_modes": ("FOK", "IOC"),
        "max_order_volume": 0.01,
        "max_total_open_volume": 0.01,
        "max_open_positions": 1,
        "max_executions_in_window": 1,
        "controlled_window_seconds": 300,
        "max_monetary_risk_per_execution": 100.0,
        "max_aggregate_monetary_risk": 100.0,
        "max_stop_distance": 20.0,
        "max_order_notional": 5_000.0,
        "max_market_age_seconds": 30,
        "effective_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return ControlledDemoExecutionPolicy(**values)


def _payload(policy: ControlledDemoExecutionPolicy, **changes: object) -> PersonalPolicyApprovalPayload:
    values: dict[str, object] = {
        "approval_id": "approval-personal-000000000000000001",
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "policy_fingerprint": policy.fingerprint,
        "trust_key_id": "test-ed25519-key-1",
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=10),
        "policy": policy,
    }
    values.update(changes)
    return PersonalPolicyApprovalPayload(**values)


class _OpenSslTestOnlyProvider:
    """Test-only Ed25519 implementation; never installed in application composition."""

    provider_id = "OPENSSL_TEST_ONLY_PROVIDER"

    def __init__(self, directory: Path, key_id: str = "TEST-ED25519-KEY-1") -> None:
        executable = shutil.which("openssl")
        if executable is None:
            pytest.skip("OpenSSL is unavailable for the isolated Ed25519 test")
        self._openssl = executable
        self._directory = directory
        self._private = directory / "temporary-test-private-key.pem"
        self._public = directory / "temporary-test-public-key.pem"
        subprocess.run(
            [self._openssl, "genpkey", "-algorithm", "ED25519", "-out", str(self._private)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                self._openssl,
                "pkey",
                "-in",
                str(self._private),
                "-pubout",
                "-out",
                str(self._public),
            ],
            check=True,
            capture_output=True,
        )
        self._descriptor = PersonalPublicKeyDescriptor(
            key_id=key_id,
            public_key_pem=self._public.read_text(encoding="ascii"),
            generation=1,
            created_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            protection=PersonalKeyProtection.TEST_ONLY,
        )

    def status(self) -> PersonalKeyProviderStatus:
        return PersonalKeyProviderStatus(
            provider_id=self.provider_id,
            availability=PersonalKeyProviderAvailability.AVAILABLE,
            protection=PersonalKeyProtection.TEST_ONLY,
            platform="TEST",
        )

    def create_key(self, *, key_id: str, created_at: datetime) -> PersonalPublicKeyDescriptor:
        if key_id.upper() != self._descriptor.key_id:
            raise KeyError(key_id)
        return self._descriptor

    def export_public_key(self, *, key_id: str) -> PersonalPublicKeyDescriptor:
        if key_id.upper() != self._descriptor.key_id:
            raise KeyError(key_id)
        return self._descriptor

    def sign(self, *, key_id: str, payload: bytes) -> bytes:
        self.export_public_key(key_id=key_id)
        payload_path = self._directory / "temporary-test-payload.bin"
        payload_path.write_bytes(payload)
        result = subprocess.run(
            [
                self._openssl,
                "pkeyutl",
                "-sign",
                "-inkey",
                str(self._private),
                "-rawin",
                "-in",
                str(payload_path),
            ],
            check=True,
            capture_output=True,
        )
        return result.stdout

    def verify(
        self, *, descriptor: PersonalPublicKeyDescriptor, payload: bytes, signature: bytes
    ) -> bool:
        if descriptor.public_key_fingerprint != self._descriptor.public_key_fingerprint:
            return False
        payload_path = self._directory / "temporary-test-payload.bin"
        signature_path = self._directory / "temporary-test-signature.bin"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        result = subprocess.run(
            [
                self._openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(self._public),
                "-sigfile",
                str(signature_path),
                "-rawin",
                "-in",
                str(payload_path),
            ],
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def rotate_key(self, *, previous_key_id: str, replacement_key_id: str, rotated_at: datetime):
        raise PersonalKeyProviderUnavailable("not needed by isolated signing tests")

    def revoke_key(self, *, key_id: str, revoked_at: datetime, reason: str):
        raise PersonalKeyProviderUnavailable("not needed by isolated signing tests")

    def recovery_status(self, *, key_id: str) -> PersonalKeyRecoveryStatus:
        return PersonalKeyRecoveryStatus(
            key_id=key_id,
            metadata_available=True,
            signing_capability_available=True,
            requires_external_recovery=False,
        )

    @property
    def descriptor(self) -> PersonalPublicKeyDescriptor:
        return self._descriptor


class _TestReplayProtector:
    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def check_and_record(self, *, approval_id: str, payload_fingerprint: str, checked_at: datetime):
        previous = self._seen.get(approval_id)
        if previous is None:
            self._seen[approval_id] = payload_fingerprint
            status = PersonalApprovalReplayStatus.ACCEPTED
            reasons = ()
        elif previous == payload_fingerprint:
            status = PersonalApprovalReplayStatus.DUPLICATE
            reasons = ("PERSONAL_APPROVAL_REPLAYED",)
        else:
            status = PersonalApprovalReplayStatus.CONFLICT
            reasons = ("PERSONAL_APPROVAL_ID_CONFLICT",)
        return PersonalApprovalReplayResult(
            status=status,
            approval_id=approval_id,
            payload_fingerprint=payload_fingerprint,
            checked_at=checked_at,
            reason_codes=reasons,
        )


def _trust(descriptor: PersonalPublicKeyDescriptor, **changes: object) -> PersonalTrustSnapshot:
    values: dict[str, object] = {
        "state": PersonalTrustState.TEST_ONLY,
        "source_id": "synthetic-test-trust",
        "active_keys": (descriptor,),
        "revocation_availability": PersonalRevocationAvailability.AVAILABLE,
        "observed_at": NOW - timedelta(seconds=1),
        "retrieved_at": NOW,
        "max_age_seconds": 30,
    }
    values.update(changes)
    return PersonalTrustSnapshot(**values)


def _signed(tmp_path: Path):
    provider = _OpenSslTestOnlyProvider(tmp_path)
    policy = _policy()
    payload = _payload(policy)
    envelope = PersonalPolicySigningService(provider).sign(payload)
    return provider, policy, payload, envelope


def test_canonical_policy_payload_is_deterministic_and_binds_full_policy() -> None:
    policy = _policy()
    first = _payload(policy)
    second = _payload(policy)
    assert canonical_personal_policy_payload(first) == canonical_personal_policy_payload(second)
    assert first.payload_fingerprint == second.payload_fingerprint
    canonical = canonical_personal_policy_payload(first)
    assert b'"max_order_volume":0.01' in canonical
    assert b'"account_reference":"Demo-Server"' in canonical
    assert b'"allowed_filling_modes":["FOK","IOC"]' in canonical
    changed = _payload(_policy(max_monetary_risk_per_execution=99.0))
    assert changed.payload_fingerprint != first.payload_fingerprint


def test_test_only_ed25519_signing_and_verification_never_authorizes(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    result = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert result.status is PersonalPolicyVerificationStatus.TEST_ONLY_VERIFIED
    assert result.cryptographic_integrity_verified is True
    assert result.independent_authority_verified is False
    assert result.production_authorization_available is False
    assert result.submission_allowed is False
    assert result.execution_authority is False


def test_payload_or_signature_tampering_fails_verification(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    tampered_policy = _policy(max_order_notional=4_999.0)
    tampered_payload = _payload(tampered_policy)
    payload_result = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=tampered_payload,
        envelope=envelope,
        expected_policy=tampered_policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert payload_result.status is PersonalPolicyVerificationStatus.REJECTED
    assert "PERSONAL_APPROVAL_PAYLOAD_FINGERPRINT_MISMATCH" in payload_result.reason_codes

    altered = bytearray(envelope.signature_bytes)
    altered[0] ^= 1
    tampered_envelope = envelope.model_copy(
        update={"signature_base64": base64.b64encode(bytes(altered)).decode("ascii")}
    )
    signature_result = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=payload,
        envelope=tampered_envelope,
        expected_policy=policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert signature_result.status is PersonalPolicyVerificationStatus.REJECTED
    assert signature_result.reason_codes == ("PERSONAL_SIGNATURE_INVALID",)


@pytest.mark.parametrize(
    "expected",
    (
        _policy(policy_version="v2"),
        _policy(
            allowed_account=SourceAccountKey(
                source_id="mt5", source_account_id="other", account_reference="Demo-Server"
            )
        ),
        _policy(risk_currency="EUR"),
    ),
)
def test_policy_version_account_and_scope_mismatch_fail_closed(tmp_path: Path, expected) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    result = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=expected,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert result.status is PersonalPolicyVerificationStatus.REJECTED
    assert "PERSONAL_POLICY_BINDING_MISMATCH" in result.reason_codes


def test_expired_and_malformed_approvals_are_rejected(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    expired = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(provider.descriptor),
        verified_at=payload.expires_at,
    )
    assert expired.status is PersonalPolicyVerificationStatus.EXPIRED
    with pytest.raises(ValidationError):
        _payload(policy, policy_fingerprint="f" * 64)
    with pytest.raises(ValidationError):
        PersonalPolicySignatureEnvelope(
            approval_id=payload.approval_id,
            key_id=payload.trust_key_id,
            payload_fingerprint=payload.payload_fingerprint,
            signature_base64="not-base64".ljust(88, "x"),
        )


def test_unknown_rotated_and_revoked_keys_fail_closed(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    replacement = provider.descriptor.model_copy(
        update={"key_id": "TEST-ED25519-KEY-2", "generation": 2}
    )
    service = PersonalPolicyVerificationService(provider, _TestReplayProtector())
    unknown = service.verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(replacement),
        verified_at=NOW,
    )
    assert "PERSONAL_TRUST_KEY_UNKNOWN_OR_ROTATED" in unknown.reason_codes
    rotated = service.verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(
            replacement,
            rotations=(
                PersonalKeyRotation(
                    previous_key_id=provider.descriptor.key_id,
                    replacement_key_id=replacement.key_id,
                    replacement_generation=2,
                    rotated_at=NOW - timedelta(seconds=1),
                    reason="synthetic test rotation",
                ),
            ),
        ),
        verified_at=NOW,
    )
    assert "PERSONAL_TRUST_KEY_UNKNOWN_OR_ROTATED" in rotated.reason_codes
    revoked = service.verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(
            replacement,
            revocations=(
                PersonalKeyRevocation(
                    key_id=provider.descriptor.key_id,
                    revoked_at=NOW - timedelta(seconds=1),
                    reason="synthetic compromise",
                ),
            ),
        ),
        verified_at=NOW,
    )
    assert revoked.status is PersonalPolicyVerificationStatus.REVOKED


def test_duplicate_approval_identifier_is_replayed_not_reauthorized(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    replay = _TestReplayProtector()
    service = PersonalPolicyVerificationService(provider, replay)
    first = service.verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    second = service.verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert first.status is PersonalPolicyVerificationStatus.TEST_ONLY_VERIFIED
    assert second.status is PersonalPolicyVerificationStatus.REPLAYED
    assert second.execution_authority is False


def test_conflicting_reuse_of_approval_identifier_fails_closed(tmp_path: Path) -> None:
    provider = _OpenSslTestOnlyProvider(tmp_path)
    replay = _TestReplayProtector()
    service = PersonalPolicyVerificationService(provider, replay)
    first_policy = _policy()
    first_payload = _payload(first_policy)
    first_envelope = PersonalPolicySigningService(provider).sign(first_payload)
    assert service.verify(
        payload=first_payload,
        envelope=first_envelope,
        expected_policy=first_policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    ).status is PersonalPolicyVerificationStatus.TEST_ONLY_VERIFIED

    changed_policy = _policy(max_order_notional=4_999.0)
    conflicting_payload = _payload(changed_policy)
    conflicting_envelope = PersonalPolicySigningService(provider).sign(conflicting_payload)
    conflict = service.verify(
        payload=conflicting_payload,
        envelope=conflicting_envelope,
        expected_policy=changed_policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert conflict.status is PersonalPolicyVerificationStatus.REPLAYED
    assert conflict.reason_codes == ("PERSONAL_APPROVAL_ID_CONFLICT",)


def test_unavailable_platform_protection_and_revocation_fail_closed(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    unavailable = UnavailableWindowsProtectedKeyProvider()
    assert unavailable.status().availability is PersonalKeyProviderAvailability.UNAVAILABLE
    assert unavailable.status().protection is PersonalKeyProtection.UNAVAILABLE
    with pytest.raises(PersonalKeyProviderUnavailable):
        PersonalPolicySigningService(unavailable).sign(payload)
    assert unavailable.recovery_status(key_id=payload.trust_key_id).private_key_material is None

    trust = _trust(
        provider.descriptor,
        revocation_availability=PersonalRevocationAvailability.UNAVAILABLE,
        reason_codes=("REVOCATION_SOURCE_UNAVAILABLE",),
    )
    result = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=trust,
        verified_at=NOW,
    )
    assert result.status is PersonalPolicyVerificationStatus.REJECTED
    assert "PERSONAL_REVOCATION_STATE_UNAVAILABLE" in result.reason_codes


def test_default_replay_protection_and_stale_trust_fail_closed(tmp_path: Path) -> None:
    provider, policy, payload, envelope = _signed(tmp_path)
    unavailable_replay = PersonalPolicyVerificationService(provider).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(provider.descriptor),
        verified_at=NOW,
    )
    assert unavailable_replay.status is PersonalPolicyVerificationStatus.UNAVAILABLE
    assert "PERSONAL_APPROVAL_REPLAY_PROTECTION_UNAVAILABLE" in unavailable_replay.reason_codes

    stale = PersonalPolicyVerificationService(provider, _TestReplayProtector()).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=_trust(
            provider.descriptor,
            observed_at=NOW - timedelta(minutes=5),
            retrieved_at=NOW - timedelta(minutes=5),
        ),
        verified_at=NOW,
    )
    assert stale.status is PersonalPolicyVerificationStatus.REJECTED
    assert "PERSONAL_TRUST_STATE_STALE" in stale.reason_codes


def test_windows_provider_lifecycle_operations_have_no_unprotected_fallback() -> None:
    provider = UnavailableWindowsProtectedKeyProvider()
    operations = (
        lambda: provider.create_key(key_id="key-1", created_at=NOW),
        lambda: provider.export_public_key(key_id="key-1"),
        lambda: provider.sign(key_id="key-1", payload=b"payload"),
        lambda: provider.verify(
            descriptor=object(), payload=b"payload", signature=b"x" * 64  # type: ignore[arg-type]
        ),
        lambda: provider.rotate_key(
            previous_key_id="key-1", replacement_key_id="key-2", rotated_at=NOW
        ),
        lambda: provider.revoke_key(key_id="key-1", revoked_at=NOW, reason="test"),
    )
    for operation in operations:
        with pytest.raises(PersonalKeyProviderUnavailable):
            operation()
    recovery = provider.recovery_status(key_id="key-1")
    assert recovery.signing_capability_available is False
    assert recovery.requires_external_recovery is True
    assert recovery.private_key_material is None


@pytest.mark.parametrize(
    "field",
    (
        "independent_authority_verified",
        "production_authorization_available",
        "submission_allowed",
        "execution_authority",
    ),
)
def test_verification_result_rejects_every_authorizing_flag(field: str) -> None:
    values = {
        "status": PersonalPolicyVerificationStatus.UNAVAILABLE,
        "approval_id": "approval-personal-000000000000000001",
        "payload_fingerprint": "a" * 64,
        "key_id": "key-1",
        "verified_at": NOW,
        "reason_codes": ("AUTHORITY_UNAVAILABLE",),
        field: True,
    }
    with pytest.raises(ValidationError):
        PersonalPolicyVerificationResult(**values)


def test_personal_signing_boundary_has_no_execution_or_mt5_path() -> None:
    source = inspect.getsource(signing_module)
    prohibited = (
        "MetaTrader5",
        "order_send",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "ExecutionStateStore",
        "ApprovalRegistry",
    )
    assert all(token not in source for token in prohibited)
