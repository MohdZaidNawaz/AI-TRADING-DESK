"""Windows-native DPAPI provider tests with temporary Ed25519 test keys."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
from datetime import timedelta
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import platform
from threading import Event
from typing import Any

import pytest

from app.services import windows_dpapi_key_provider as dpapi_module

from app.models.personal_policy_signing import (
    PersonalKeyProtection,
    PersonalKeyProviderAvailability,
    PersonalPolicyVerificationStatus,
    PersonalRevocationAvailability,
    PersonalTrustSnapshot,
    PersonalTrustState,
)
from app.services.personal_policy_signing import (
    PersonalKeyProviderUnavailable,
    PersonalPolicySigningService,
    PersonalPolicyVerificationService,
)
from app.services.windows_dpapi_key_provider import (
    CryptographyEd25519Backend,
    WindowsDpapiKeyProvider,
    WindowsDpapiProtector,
    WindowsRestrictedAcl,
    _DataBlob,
    _StoredKeyRecord,
    _record_entropy,
    _zero,
)
from tests.test_personal_policy_signing import NOW, _TestReplayProtector, _payload, _policy


pytestmark = pytest.mark.skipif(platform.system() != "Windows", reason="Windows DPAPI only")


class _PassThroughProtector:
    def protect(self, plaintext: bytearray, *, entropy: bytes) -> bytes:
        del entropy
        return bytes(plaintext)

    def unprotect(self, protected_blob: bytes, *, entropy: bytes) -> bytearray:
        del entropy
        return bytearray(protected_blob)


class _PermissiveAcl:
    def secure(self, path: Path) -> None:
        del path

    def verify(self, path: Path) -> bool:
        del path
        return True


class _TrackingLocalFree:
    def __init__(self) -> None:
        self.calls = 0

    def LocalFree(self, pointer: object) -> None:
        assert pointer
        self.calls += 1
        return None


def _native_test_blob(value: bytes) -> tuple[_DataBlob, object]:
    backing = (ctypes.c_ubyte * len(value))(*value)
    return (
        _DataBlob(len(backing), ctypes.cast(backing, ctypes.POINTER(ctypes.c_ubyte))),
        backing,
    )


def _provider(directory: Path) -> WindowsDpapiKeyProvider:
    return WindowsDpapiKeyProvider(storage_dir=directory / "keys")


def _key_file(directory: Path) -> Path:
    files = tuple((directory / "keys").glob("*.key.json"))
    assert len(files) == 1
    return files[0]


def _legacy_record_entropy(record: _StoredKeyRecord) -> bytes:
    binding = {
        "context": "NimbusDesk/FRIDAY/personal-policy-key/v1",
        "schema_version": record.schema_version,
        "key_id": record.descriptor.key_id,
        "generation": record.descriptor.generation,
        "public_key_fingerprint": record.descriptor.public_key_fingerprint,
        "protection": record.descriptor.protection.value,
        "state": record.state,
        "state_changed_at": record.state_changed_at.isoformat(),
    }
    return sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).digest()


def _rotate_worker(
    storage_dir: str,
    replacement_key_id: str,
    start_event: Any,
    results: Any,
) -> None:
    """Spawn-safe worker exercising the real inter-process lifecycle lock."""
    provider = WindowsDpapiKeyProvider(storage_dir=Path(storage_dir))
    start_event.wait(timeout=10)
    try:
        provider.rotate_key(
            previous_key_id="personal-key-1",
            replacement_key_id=replacement_key_id,
            rotated_at=NOW + timedelta(seconds=1),
        )
    except PersonalKeyProviderUnavailable:
        results.put(("rejected", replacement_key_id))
    else:
        results.put(("completed", replacement_key_id))


def _create_worker(storage_dir: str, start_event: Any, results: Any) -> None:
    """Spawn-safe initialization and duplicate-creation worker."""
    provider = WindowsDpapiKeyProvider(storage_dir=Path(storage_dir))
    start_event.wait(timeout=10)
    try:
        provider.create_key(key_id="personal-key-1", created_at=NOW)
    except PersonalKeyProviderUnavailable:
        results.put("rejected")
    else:
        results.put("completed")


def _revoke_worker(storage_dir: str, start_event: Any, results: Any) -> None:
    """Spawn-safe revocation worker for a rotation/revocation race."""
    provider = WindowsDpapiKeyProvider(storage_dir=Path(storage_dir))
    start_event.wait(timeout=10)
    try:
        provider.revoke_key(
            key_id="personal-key-1",
            revoked_at=NOW + timedelta(seconds=1),
            reason="explicit concurrent revocation test",
        )
    except PersonalKeyProviderUnavailable:
        results.put(("rejected", "revoke"))
    else:
        results.put(("completed", "revoke"))


def test_native_dpapi_current_user_round_trip_and_entropy_binding() -> None:
    protector = WindowsDpapiProtector()
    plaintext = bytearray(b"temporary-test-secret-material")
    protected = protector.protect(plaintext, entropy=b"friday-test-context")
    assert protected != bytes(plaintext)
    recovered = protector.unprotect(protected, entropy=b"friday-test-context")
    assert recovered == plaintext
    recovered[:] = b"\x00" * len(recovered)
    with pytest.raises(PersonalKeyProviderUnavailable):
        protector.unprotect(protected, entropy=b"wrong-context")


def test_native_dpapi_rejects_tampered_and_malformed_blobs() -> None:
    protector = WindowsDpapiProtector()
    protected = bytearray(
        protector.protect(bytearray(b"temporary-test-secret"), entropy=b"context")
    )
    protected[len(protected) // 2] ^= 1
    with pytest.raises(PersonalKeyProviderUnavailable):
        protector.unprotect(bytes(protected), entropy=b"context")
    for malformed in (b"", b"not-a-dpapi-blob"):
        with pytest.raises(PersonalKeyProviderUnavailable):
            protector.unprotect(malformed, entropy=b"context")


def test_native_dpapi_rejects_invalid_sizes_before_native_use() -> None:
    protector = WindowsDpapiProtector()
    with pytest.raises(PersonalKeyProviderUnavailable, match="size"):
        protector.protect(bytearray(), entropy=b"context")
    with pytest.raises(PersonalKeyProviderUnavailable, match="size"):
        protector.protect(bytearray(64 * 1024 + 1), entropy=b"context")
    with pytest.raises(PersonalKeyProviderUnavailable, match="size"):
        protector.unprotect(b"x" * (1024 * 1024 + 1), entropy=b"context")
    with pytest.raises(PersonalKeyProviderUnavailable, match="entropy"):
        protector.protect(bytearray(b"key"), entropy=b"")


@pytest.mark.parametrize("operation", ("protect", "unprotect"))
def test_native_dpapi_frees_partial_output_on_native_failure(operation: str) -> None:
    class _FailingCrypt32:
        def __init__(self) -> None:
            self.output = (ctypes.c_ubyte * 16)(*range(16))

        def _fail(self, *args: object) -> int:
            output = ctypes.cast(args[-1], ctypes.POINTER(_DataBlob)).contents
            output.cbData = len(self.output)
            output.pbData = ctypes.cast(
                self.output, ctypes.POINTER(ctypes.c_ubyte)
            )
            ctypes.set_last_error(13)
            return 0

        CryptProtectData = _fail
        CryptUnprotectData = _fail

    class _TrackingKernel32:
        def __init__(self) -> None:
            self.freed = 0

        def LocalFree(self, pointer: object) -> None:
            assert pointer
            self.freed += 1
            return None

    protector = WindowsDpapiProtector.__new__(WindowsDpapiProtector)
    crypt32 = _FailingCrypt32()
    kernel32 = _TrackingKernel32()
    protector._crypt32 = crypt32  # type: ignore[attr-defined]
    protector._kernel32 = kernel32  # type: ignore[attr-defined]

    with pytest.raises(PersonalKeyProviderUnavailable, match="Windows DPAPI"):
        if operation == "protect":
            protector.protect(bytearray(b"known private material"), entropy=b"context")
        else:
            protector.unprotect(b"synthetic protected blob", entropy=b"context")

    assert kernel32.freed == 1
    if operation == "unprotect":
        assert bytes(crypt32.output) == b"\x00" * len(crypt32.output)


def test_native_copy_allocation_failure_still_frees_secret_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "DO-NOT-EXPOSE-PRIVATE-ALLOCATION-DIAGNOSTIC"
    protector = WindowsDpapiProtector.__new__(WindowsDpapiProtector)
    kernel32 = _TrackingLocalFree()
    protector._kernel32 = kernel32  # type: ignore[attr-defined]
    blob, backing = _native_test_blob(b"temporary-private-material")

    def fail_allocation(size: int) -> bytearray:
        del size
        raise MemoryError(marker)

    monkeypatch.setattr(dpapi_module, "bytearray", fail_allocation, raising=False)
    with pytest.raises(PersonalKeyProviderUnavailable, match="output copy failed") as failure:
        protector._copy_and_free(blob, secret=True)  # type: ignore[attr-defined]

    assert kernel32.calls == 1
    assert bytes(backing) == b"\x00" * len(backing)
    assert marker not in str(failure.value)
    assert marker not in repr(failure.value)
    assert failure.value.__cause__ is None


def test_native_copy_failure_still_frees_secret_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "DO-NOT-EXPOSE-PRIVATE-COPY-DIAGNOSTIC"
    protector = WindowsDpapiProtector.__new__(WindowsDpapiProtector)
    kernel32 = _TrackingLocalFree()
    protector._kernel32 = kernel32  # type: ignore[attr-defined]
    blob, backing = _native_test_blob(b"temporary-private-material")

    def fail_copy(*args: object) -> None:
        del args
        raise RuntimeError(marker)

    monkeypatch.setattr(dpapi_module.ctypes, "memmove", fail_copy)
    with pytest.raises(PersonalKeyProviderUnavailable, match="output copy failed") as failure:
        protector._copy_and_free(blob, secret=True)  # type: ignore[attr-defined]

    assert kernel32.calls == 1
    assert bytes(backing) == b"\x00" * len(backing)
    assert marker not in str(failure.value)
    assert marker not in repr(failure.value)
    assert failure.value.__cause__ is None


def test_native_zeroing_failure_still_attempts_local_free_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "DO-NOT-EXPOSE-PRIVATE-ZEROING-DIAGNOSTIC"
    protector = WindowsDpapiProtector.__new__(WindowsDpapiProtector)
    kernel32 = _TrackingLocalFree()
    protector._kernel32 = kernel32  # type: ignore[attr-defined]
    blob, _ = _native_test_blob(b"temporary-private-material")

    def fail_zero(*args: object) -> None:
        del args
        raise RuntimeError(marker)

    monkeypatch.setattr(dpapi_module.ctypes, "memset", fail_zero)
    with pytest.raises(PersonalKeyProviderUnavailable, match="cleanup failed") as failure:
        protector._free_native(blob, secret=True)  # type: ignore[attr-defined]

    assert kernel32.calls == 1
    assert not blob.pbData
    assert blob.cbData == 0
    assert marker not in str(failure.value)
    assert marker not in repr(failure.value)
    assert failure.value.__cause__ is None


def test_signing_and_verification_survive_provider_restart(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    assert provider.status().availability is PersonalKeyProviderAvailability.AVAILABLE
    assert provider.status().protection is PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER
    descriptor = provider.create_key(key_id="personal-key-1", created_at=NOW - timedelta(minutes=5))
    assert descriptor.protection is PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER

    restarted = _provider(tmp_path)
    assert restarted.export_public_key(key_id=descriptor.key_id) == descriptor
    policy = _policy()
    payload = _payload(policy, trust_key_id=descriptor.key_id)
    envelope = PersonalPolicySigningService(restarted).sign(payload)
    assert restarted.verify(
        descriptor=descriptor,
        payload=payload.model_dump_json().encode(),
        signature=b"x" * 64,
    ) is False

    trust = PersonalTrustSnapshot(
        state=PersonalTrustState.LOCAL_DECLARATION_ONLY,
        source_id="local-dpapi-test",
        active_keys=(descriptor,),
        revocation_availability=PersonalRevocationAvailability.AVAILABLE,
        observed_at=NOW - timedelta(seconds=1),
        retrieved_at=NOW,
        max_age_seconds=30,
    )
    result = PersonalPolicyVerificationService(
        restarted, _TestReplayProtector()
    ).verify(
        payload=payload,
        envelope=envelope,
        expected_policy=policy,
        trust=trust,
        verified_at=NOW,
    )
    assert result.status is PersonalPolicyVerificationStatus.UNAVAILABLE
    assert result.cryptographic_integrity_verified is True
    assert result.production_authorization_available is False
    assert result.execution_authority is False


def test_repeated_creation_never_replaces_existing_key(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    descriptor = provider.create_key(key_id="personal-key-1", created_at=NOW)
    record = _key_file(tmp_path)
    original_bytes = record.read_bytes()
    with pytest.raises(PersonalKeyProviderUnavailable, match="already exists"):
        _provider(tmp_path).create_key(
            key_id="personal-key-1", created_at=NOW + timedelta(seconds=1)
        )
    assert record.read_bytes() == original_bytes
    assert provider.export_public_key(key_id=descriptor.key_id) == descriptor


def test_known_private_key_fixture_never_appears_in_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_private_key = bytes(range(32))

    def generate_known_pair(
        backend: CryptographyEd25519Backend,
    ) -> tuple[bytearray, str]:
        private_key = bytearray(known_private_key)
        return private_key, backend.public_key_from_private(private_key)

    monkeypatch.setattr(
        CryptographyEd25519Backend,
        "generate_key_pair",
        generate_known_pair,
    )
    provider = _provider(tmp_path)
    provider.create_key(key_id="known-personal-key", created_at=NOW)

    forbidden = (
        known_private_key,
        base64.b64encode(known_private_key),
        known_private_key.hex().encode("ascii"),
    )
    for stored_path in (tmp_path / "keys").iterdir():
        if stored_path.is_file():
            stored = stored_path.read_bytes()
            assert all(candidate not in stored for candidate in forbidden)


def test_corrupted_record_and_ciphertext_fail_without_replacement(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    descriptor = provider.create_key(key_id="personal-key-1", created_at=NOW)
    record_path = _key_file(tmp_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    ciphertext = bytearray(base64.b64decode(record["protected_private_key_base64"], validate=True))
    ciphertext[len(ciphertext) // 2] ^= 1
    record["protected_private_key_base64"] = base64.b64encode(ciphertext).decode("ascii")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    WindowsRestrictedAcl().secure(record_path)
    with pytest.raises(PersonalKeyProviderUnavailable):
        provider.sign(key_id=descriptor.key_id, payload=b"payload")
    recovery = provider.recovery_status(key_id=descriptor.key_id)
    assert recovery.signing_capability_available is False
    assert recovery.requires_external_recovery is True
    assert tuple((tmp_path / "keys").glob("*.key.json")) == (record_path,)


@pytest.mark.parametrize(
    "field",
    (
        "record-schema",
        "descriptor-schema",
        "algorithm",
        "key-id",
        "generation",
        "protection",
        "created-at",
        "expires-at",
        "state-changed-at",
        "lifecycle-state",
    ),
)
def test_every_security_relevant_record_field_is_validated_or_entropy_bound(
    tmp_path: Path, field: str
) -> None:
    provider = _provider(tmp_path)
    descriptor = provider.create_key(key_id="personal-key-1", created_at=NOW)
    record_path = _key_file(tmp_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if field == "record-schema":
        record["schema_version"] = "windows-dpapi-personal-key/v0"
    elif field == "descriptor-schema":
        record["descriptor"]["schema_version"] = "personal-public-key/v0"
    elif field == "algorithm":
        record["descriptor"]["algorithm"] = "RSA"
    elif field == "key-id":
        record["descriptor"]["key_id"] = "OTHER-KEY"
    elif field == "generation":
        record["descriptor"]["generation"] = 2
    elif field == "protection":
        record["descriptor"]["protection"] = "test_only"
    elif field == "created-at":
        record["descriptor"]["created_at"] = (NOW - timedelta(days=1)).isoformat()
    elif field == "expires-at":
        record["descriptor"]["expires_at"] = (NOW + timedelta(days=1)).isoformat()
    elif field == "state-changed-at":
        record["state_changed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif field == "lifecycle-state":
        record["state"] = "revoked"
        record["state_reason"] = "synthetic tamper"
    original_tampered = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    record_path.write_bytes(original_tampered)
    WindowsRestrictedAcl().secure(record_path)
    with pytest.raises(PersonalKeyProviderUnavailable):
        provider.sign(key_id=descriptor.key_id, payload=b"payload")
    assert record_path.read_bytes() == original_tampered
    assert provider.recovery_status(key_id=descriptor.key_id).requires_external_recovery is True


def test_legacy_entropy_record_fails_closed_without_rewrite(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    descriptor = provider.create_key(key_id="personal-key-1", created_at=NOW)
    record_path = _key_file(tmp_path)
    record_json = json.loads(record_path.read_text(encoding="utf-8"))
    record = _StoredKeyRecord.model_validate(record_json)
    protector = WindowsDpapiProtector()
    private_key = protector.unprotect(
        base64.b64decode(record.protected_private_key_base64, validate=True),
        entropy=_record_entropy(record),
    )
    try:
        legacy_blob = protector.protect(private_key, entropy=_legacy_record_entropy(record))
    finally:
        _zero(private_key)
    record_json["protected_private_key_base64"] = base64.b64encode(legacy_blob).decode("ascii")
    legacy_bytes = (json.dumps(record_json, sort_keys=True) + "\n").encode("utf-8")
    record_path.write_bytes(legacy_bytes)
    WindowsRestrictedAcl().secure(record_path)
    with pytest.raises(PersonalKeyProviderUnavailable):
        provider.sign(key_id=descriptor.key_id, payload=b"payload")
    assert record_path.read_bytes() == legacy_bytes
    assert provider.recovery_status(key_id=descriptor.key_id).requires_external_recovery is True


def test_missing_invalid_key_and_failed_native_protection_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    missing = provider.recovery_status(key_id="missing-key")
    assert missing.metadata_available is False
    assert missing.signing_capability_available is False
    with pytest.raises(PersonalKeyProviderUnavailable):
        provider.export_public_key(key_id="../escape")

    failing = WindowsDpapiKeyProvider(storage_dir=tmp_path / "failing-keys")
    assert failing.status().availability is PersonalKeyProviderAvailability.AVAILABLE
    monkeypatch.setattr(
        WindowsDpapiProtector,
        "protect",
        lambda self, plaintext, *, entropy: (_ for _ in ()).throw(
            PersonalKeyProviderUnavailable("synthetic DPAPI failure")
        ),
    )
    with pytest.raises(PersonalKeyProviderUnavailable):
        failing.create_key(key_id="personal-key-1", created_at=NOW)
    assert not tuple((tmp_path / "failing-keys").glob("*.key.json"))


def test_rotation_revocation_and_recovery_are_explicit_and_persistent(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    original = provider.create_key(key_id="personal-key-1", created_at=NOW - timedelta(minutes=2))
    replacement, rotation = provider.rotate_key(
        previous_key_id=original.key_id,
        replacement_key_id="personal-key-2",
        rotated_at=NOW - timedelta(minutes=1),
    )
    assert rotation.replacement_key_id == replacement.key_id
    with pytest.raises(PersonalKeyProviderUnavailable, match="not active"):
        provider.sign(key_id=original.key_id, payload=b"payload")
    assert provider.recovery_status(key_id=original.key_id).signing_capability_available is False
    assert provider.recovery_status(key_id=replacement.key_id).signing_capability_available is True
    assert len(provider.sign(key_id=replacement.key_id, payload=b"payload")) == 64

    revocation = provider.revoke_key(
        key_id=replacement.key_id,
        revoked_at=NOW,
        reason="explicit synthetic compromise response",
    )
    assert revocation.key_id == replacement.key_id
    restarted = _provider(tmp_path)
    assert restarted.recovery_status(key_id=replacement.key_id).signing_capability_available is False
    with pytest.raises(PersonalKeyProviderUnavailable, match="not active"):
        restarted.sign(key_id=replacement.key_id, payload=b"payload")


def test_interrupted_rotation_fails_closed_without_two_active_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    provider.create_key(key_id="personal-key-1", created_at=NOW)

    def fail_replacement_write(path: Path, record: _StoredKeyRecord) -> None:
        del path, record
        raise PersonalKeyProviderUnavailable("synthetic replacement persistence failure")

    monkeypatch.setattr(provider, "_write_new", fail_replacement_write)
    with pytest.raises(PersonalKeyProviderUnavailable, match="rotation failed"):
        provider.rotate_key(
            previous_key_id="personal-key-1",
            replacement_key_id="personal-key-2",
            rotated_at=NOW + timedelta(seconds=1),
        )

    restarted = _provider(tmp_path)
    assert restarted.recovery_status(key_id="personal-key-1").signing_capability_available is False
    assert restarted.recovery_status(key_id="personal-key-2").metadata_available is False


def test_cross_process_initialization_and_creation_are_serialized(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "keys"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = tuple(
        context.Process(
            target=_create_worker,
            args=(str(storage_dir), start_event, results),
        )
        for _ in range(2)
    )
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=5) for _ in processes) == ["completed", "rejected"]
    restarted = WindowsDpapiKeyProvider(storage_dir=storage_dir)
    assert restarted.recovery_status(key_id="personal-key-1").signing_capability_available


def test_cross_process_rotations_are_serialized_and_fail_closed(tmp_path: Path) -> None:
    storage_dir = tmp_path / "keys"
    _provider(tmp_path).create_key(key_id="personal-key-1", created_at=NOW)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = (
        context.Process(
            target=_rotate_worker,
            args=(str(storage_dir), "personal-key-2", start_event, results),
        ),
        context.Process(
            target=_rotate_worker,
            args=(str(storage_dir), "personal-key-3", start_event, results),
        ),
    )
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    outcomes = sorted(results.get(timeout=5) for _ in processes)
    assert [outcome for outcome, _ in outcomes] == ["completed", "rejected"]
    winner = next(key_id for outcome, key_id in outcomes if outcome == "completed")
    loser = next(key_id for outcome, key_id in outcomes if outcome == "rejected")
    restarted = WindowsDpapiKeyProvider(storage_dir=storage_dir)
    assert restarted.recovery_status(key_id="personal-key-1").signing_capability_available is False
    assert restarted.recovery_status(key_id=winner).signing_capability_available is True
    assert restarted.recovery_status(key_id=loser).metadata_available is False


def test_cross_process_rotation_and_revocation_cannot_both_succeed(
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "keys"
    _provider(tmp_path).create_key(key_id="personal-key-1", created_at=NOW)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = (
        context.Process(
            target=_rotate_worker,
            args=(str(storage_dir), "personal-key-2", start_event, results),
        ),
        context.Process(
            target=_revoke_worker,
            args=(str(storage_dir), start_event, results),
        ),
    )
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    outcomes = sorted(results.get(timeout=5) for _ in processes)
    assert [outcome for outcome, _ in outcomes] == ["completed", "rejected"]
    restarted = WindowsDpapiKeyProvider(storage_dir=storage_dir)
    original = restarted.recovery_status(key_id="personal-key-1")
    replacement = restarted.recovery_status(key_id="personal-key-2")
    assert original.signing_capability_available is False
    assert sum(
        int(value)
        for value in (
            original.signing_capability_available,
            replacement.signing_capability_available,
        )
    ) <= 1


def test_malformed_lifecycle_lock_blocks_existing_and_restarted_providers(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    descriptor = provider.create_key(key_id="personal-key-1", created_at=NOW)
    lock_path = tmp_path / "keys" / ".personal-policy-key-lifecycle.lock"
    lock_path.write_bytes(b"malformed")
    WindowsRestrictedAcl().secure(lock_path)

    with pytest.raises(PersonalKeyProviderUnavailable, match="lock"):
        provider.export_public_key(key_id=descriptor.key_id)
    restarted = _provider(tmp_path)
    assert restarted.status().availability is PersonalKeyProviderAvailability.UNAVAILABLE


def test_lifecycle_lock_timeout_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _provider(tmp_path)
    descriptor = first.create_key(key_id="personal-key-1", created_at=NOW)
    second = _provider(tmp_path)
    acquired = Event()
    release = Event()

    def hold_lock() -> None:
        with first._exclusive_access():  # type: ignore[attr-defined]
            acquired.set()
            release.wait(timeout=5)

    monkeypatch.setattr(dpapi_module, "_LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.05)
    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_lock)
        assert acquired.wait(timeout=5)
        try:
            with pytest.raises(PersonalKeyProviderUnavailable, match="timed out"):
                second.export_public_key(key_id=descriptor.key_id)
        finally:
            release.set()
        holder.result(timeout=5)


def test_storage_acl_is_restrictive_and_record_contains_no_plaintext_key(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider.create_key(key_id="personal-key-1", created_at=NOW)
    record = _key_file(tmp_path)
    acl = WindowsRestrictedAcl()
    assert acl.verify(tmp_path / "keys") is True
    assert acl.verify(record) is True
    inherited = tmp_path / "keys" / "inheritance-check.txt"
    inherited.write_text("non-secret", encoding="ascii")
    assert acl.verify(inherited) is False
    acl.secure(inherited)
    assert acl.verify(inherited) is True
    text = record.read_text(encoding="utf-8")
    assert "BEGIN PRIVATE KEY" not in text
    assert "PRIVATE KEY" not in text
    assert "protected_private_key_base64" in text


@pytest.mark.parametrize(
    ("sddl_factory", "protected", "target_kind"),
    (
        (
            lambda sid: (
                f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
                "(A;;GR;;;S-1-5-32-546)"
            ),
            True,
            "record",
        ),
        (lambda sid: f"D:P(A;;GR;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)", True, "record"),
        (
            lambda sid: (
                f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
                "(D;;FA;;;S-1-5-32-546)"
            ),
            True,
            "record",
        ),
        (
            lambda sid: f"D:P(A;OICI;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)",
            True,
            "directory",
        ),
        (lambda sid: f"D:(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)", False, "record"),
        (
            lambda sid: (
                f"D:P(A;;FA;;;{sid})(A;;GR;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
            ),
            True,
            "record",
        ),
        (
            lambda sid: (
                f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"
            ),
            True,
            "record",
        ),
    ),
    ids=(
        "extra-arbitrary-sid",
        "incorrect-rights",
        "deny-ace",
        "unexpected-file-inheritance",
        "unprotected-dacl",
        "duplicate-ace",
        "broad-principal",
    ),
)
def test_acl_verification_rejects_every_noncanonical_descriptor(
    tmp_path: Path, sddl_factory, protected: bool, target_kind: str
) -> None:
    provider = _provider(tmp_path)
    provider.create_key(key_id="personal-key-1", created_at=NOW)
    record = _key_file(tmp_path)
    acl = WindowsRestrictedAcl()
    target = record if target_kind == "record" else tmp_path / "keys"
    acl._apply_dacl(  # type: ignore[attr-defined]
        target,
        sddl_factory(acl.current_user_sid),
        protected=protected,
    )
    assert acl.verify(target) is False
    with pytest.raises(PersonalKeyProviderUnavailable):
        provider.export_public_key(key_id="personal-key-1")


def test_provider_errors_do_not_expose_backend_or_private_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "DO-NOT-EXPOSE-PRIVATE-MATERIAL"
    provider = WindowsDpapiKeyProvider(storage_dir=tmp_path / "keys")
    monkeypatch.setattr(
        WindowsDpapiProtector,
        "protect",
        lambda self, plaintext, *, entropy: (_ for _ in ()).throw(
            PersonalKeyProviderUnavailable(f"synthetic failure {marker}")
        ),
    )
    with pytest.raises(PersonalKeyProviderUnavailable) as failure:
        provider.create_key(key_id="personal-key-1", created_at=NOW)
    assert marker not in str(failure.value)
    assert marker not in repr(failure.value)
    assert failure.value.__cause__ is None
    assert not tuple((tmp_path / "keys").glob("*.key.json"))


def test_production_constructor_rejects_injected_protection_dependencies(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        WindowsDpapiKeyProvider(  # type: ignore[call-arg]
            storage_dir=tmp_path / "keys",
            protector=_PassThroughProtector(),
        )
    with pytest.raises(TypeError):
        WindowsDpapiKeyProvider(  # type: ignore[call-arg]
            storage_dir=tmp_path / "keys",
            acl=_PermissiveAcl(),
        )
    assert not (tmp_path / "keys").exists()


def test_provider_is_unavailable_off_windows_without_creating_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    provider = WindowsDpapiKeyProvider(storage_dir=tmp_path / "keys")
    status = provider.status()
    assert status.availability is PersonalKeyProviderAvailability.UNAVAILABLE
    assert status.protection is PersonalKeyProtection.UNAVAILABLE
    assert status.production_trust_established is False
    with pytest.raises(PersonalKeyProviderUnavailable):
        provider.create_key(key_id="personal-key-1", created_at=NOW)
    assert not (tmp_path / "keys").exists()
