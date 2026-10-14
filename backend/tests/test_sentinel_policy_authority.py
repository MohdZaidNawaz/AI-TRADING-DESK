"""Policy governance tests use local SQLite and synthetic records only."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.sentinel_policy_authority import (
    SentinelPolicyAuthorityLevel,
    SentinelPolicyLifecycle,
    SentinelPolicyProvenance,
    SentinelPolicyResolutionStatus,
    SentinelPolicySourceKind,
    SentinelPortfolioRiskPolicyRecord,
)
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalArtifact,
    SentinelPolicyApprovalLifecycle,
    SentinelPolicyApprovalMechanism,
    SentinelPolicyApprovalReadiness,
    SentinelPolicyApprovalScope,
    SentinelPolicyApprovalState,
    SentinelPolicyApprovalVerification,
    SentinelPolicyApprovalVerificationStatus,
)
from app.services.sentinel_policy_approval import approval_binding_codes
from app.services.sentinel_policy_authority import (
    SentinelPortfolioRiskPolicyVerifier,
    TestOnlySentinelPolicySource,
)
from app.services.sentinel_policy_catalog import (
    LocalSentinelPolicyCatalog,
    SentinelPolicyCatalogError,
)
from tests.test_demo_execution_readiness import NOW, _policy


def _bounded_policy(**changes: object) -> ControlledDemoExecutionPolicy:
    return _policy(
        effective_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        **changes,
    )


def _test_policy_record(
    policy: ControlledDemoExecutionPolicy | None = None,
    **changes: object,
) -> SentinelPortfolioRiskPolicyRecord:
    policy = policy or _bounded_policy()
    values: dict[str, object] = {
        "policy": policy,
        "lifecycle": SentinelPolicyLifecycle.ACTIVE,
        "lifecycle_version": 1,
        "authority_level": SentinelPolicyAuthorityLevel.TEST_ONLY,
        "provenance": SentinelPolicyProvenance(
            source_id="pytest-policy-fixture",
            source_kind=SentinelPolicySourceKind.TEST_FIXTURE,
            source_version="v1",
        ),
        "approval": _test_approval_state(policy),
        "registered_at": NOW - timedelta(minutes=1),
        "lifecycle_changed_at": NOW - timedelta(minutes=1),
    }
    values.update(changes)
    return SentinelPortfolioRiskPolicyRecord(**values)


def _test_approval_state(
    policy: ControlledDemoExecutionPolicy,
    **changes: object,
) -> SentinelPolicyApprovalState:
    artifact_values: dict[str, object] = {
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "policy_fingerprint": policy.fingerprint,
        "approver_id": "PYTEST-INDEPENDENT-APPROVER",
        "mechanism": SentinelPolicyApprovalMechanism.TEST_FIXTURE,
        "proof": "PYTEST-ONLY-VERIFIED-PROOF",
        "approved_at": NOW - timedelta(minutes=1),
        "effective_at": NOW - timedelta(minutes=2),
        "expires_at": NOW + timedelta(minutes=10),
        "scope": SentinelPolicyApprovalScope(
            account_ids=(policy.allowed_account.account_id,),
            execution_modes=(policy.mode.value,),
            canonical_symbols=(policy.instrument.canonical_symbol,),
            risk_currencies=(policy.risk_currency,),
        ),
    }
    state_changes = {
        key: changes.pop(key)
        for key in tuple(changes)
        if key in {"lifecycle", "lifecycle_version", "registered_at", "lifecycle_changed_at", "revoked_reason", "superseded_by_artifact_id"}
    }
    artifact_values.update(changes)
    artifact = SentinelPolicyApprovalArtifact(**artifact_values)
    state_values: dict[str, object] = {
        "artifact": artifact,
        "registered_at": NOW - timedelta(minutes=1),
        "lifecycle_changed_at": NOW - timedelta(minutes=1),
    }
    state_values.update(state_changes)
    return SentinelPolicyApprovalState(**state_values)


class _TestApprovalVerifier:
    """Test-file-only verifier; its result can never be production authority."""

    def verify(self, *, approval, policy, policy_source_configured, evaluated_at):
        codes = list(approval_binding_codes(
            approval=approval, policy=policy, evaluated_at=evaluated_at
        ))
        if approval.artifact.approver_id != "PYTEST-INDEPENDENT-APPROVER":
            codes.append("SENTINEL_POLICY_APPROVER_UNAUTHORIZED")
        if approval.artifact.proof != "PYTEST-ONLY-VERIFIED-PROOF":
            codes.append("SENTINEL_POLICY_APPROVAL_PROOF_INVALID")
        if codes:
            return SentinelPolicyApprovalVerification(
                status=SentinelPolicyApprovalVerificationStatus.REJECTED,
                artifact_id=approval.artifact.approval_artifact_id,
                artifact_fingerprint=approval.artifact.canonical_payload_fingerprint,
                approver_id=approval.artifact.approver_id,
                verified_at=evaluated_at,
                readiness=SentinelPolicyApprovalReadiness(
                    policy_source_configured=policy_source_configured,
                    approval_artifact_available=True,
                ),
                reason_codes=tuple(codes),
            )
        return SentinelPolicyApprovalVerification(
            status=SentinelPolicyApprovalVerificationStatus.TEST_ONLY_VERIFIED,
            artifact_id=approval.artifact.approval_artifact_id,
            artifact_fingerprint=approval.artifact.canonical_payload_fingerprint,
            approver_id=approval.artifact.approver_id,
            verified_at=evaluated_at,
            readiness=SentinelPolicyApprovalReadiness(
                policy_source_configured=policy_source_configured,
                governance_contract_configured=True,
                approval_source_configured=True,
                revocation_status_available=True,
                trust_anchor_configured=False,
                approval_artifact_available=True,
                approver_verified=True,
                policy_fingerprint_verified=True,
                approval_lifecycle_valid=True,
                approval_scope_valid=True,
                production_policy_resolution_available=False,
            ),
        )


def _test_policy_verifier(
    records: tuple[SentinelPortfolioRiskPolicyRecord, ...],
) -> SentinelPortfolioRiskPolicyVerifier:
    return SentinelPortfolioRiskPolicyVerifier.for_test(
        records, approval_verifier=_TestApprovalVerifier()
    )


def _local_policy_record(
    policy: ControlledDemoExecutionPolicy | None = None,
    **changes: object,
) -> SentinelPortfolioRiskPolicyRecord:
    policy = policy or _bounded_policy()
    values: dict[str, object] = {
        "policy": policy,
        "lifecycle": SentinelPolicyLifecycle.ACTIVE,
        "lifecycle_version": 1,
        "authority_level": SentinelPolicyAuthorityLevel.LOCAL_GOVERNANCE_ONLY,
        "provenance": SentinelPolicyProvenance(
            source_id="local-policy-catalog",
            source_kind=SentinelPolicySourceKind.LOCAL_SQLITE_CATALOG,
            source_version="schema-v1",
        ),
        "registered_at": NOW - timedelta(minutes=1),
        "lifecycle_changed_at": NOW - timedelta(minutes=1),
    }
    values.update(changes)
    return SentinelPortfolioRiskPolicyRecord(**values)


def test_policy_record_is_immutable_and_canonicalization_is_stable() -> None:
    left = _test_policy_record()
    right = _test_policy_record()
    assert left.policy_content_fingerprint == right.policy_content_fingerprint
    assert left.record_fingerprint == right.record_fingerprint
    with pytest.raises(ValidationError):
        left.lifecycle = SentinelPolicyLifecycle.REVOKED  # type: ignore[misc]


def test_approval_must_bind_exact_policy_content() -> None:
    policy = _bounded_policy()
    with pytest.raises(ValidationError, match="bind"):
        _test_policy_record(
            policy,
            approval=_test_approval_state(policy, policy_fingerprint="0" * 64),
        )

    payload = _test_policy_record(policy).model_dump(exclude_computed_fields=True)
    payload["approved"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        SentinelPortfolioRiskPolicyRecord.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"authority_level": SentinelPolicyAuthorityLevel.INDEPENDENTLY_APPROVED},
        {"approval": None},
        {
            "provenance": SentinelPolicyProvenance(
                source_id="local", source_kind=SentinelPolicySourceKind.LOCAL_SQLITE_CATALOG,
                source_version="v1",
            )
        },
    ],
)
def test_test_fixture_cannot_claim_independent_or_missing_approval(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _test_policy_record(**changes)


def test_default_and_arbitrary_sources_fail_closed() -> None:
    policy = _bounded_policy()
    unavailable = SentinelPortfolioRiskPolicyVerifier().resolve(candidate=policy, evaluated_at=NOW)
    assert unavailable.status is SentinelPolicyResolutionStatus.UNAVAILABLE
    assert unavailable.reason_codes == ("SENTINEL_RISK_POLICY_AUTHORITY_UNAVAILABLE",)

    class CallerControlledSource:
        source_id = "caller-controlled"

        def resolve(self, *, policy_id: str, policy_version: str):
            del policy_id, policy_version
            return _test_policy_record(policy)

    forged = SentinelPortfolioRiskPolicyVerifier(CallerControlledSource()).resolve(
        candidate=policy, evaluated_at=NOW
    )
    assert forged.status is SentinelPolicyResolutionStatus.UNAVAILABLE
    assert forged.reason_codes == ("SENTINEL_RISK_POLICY_SOURCE_UNTRUSTED",)


def test_test_source_requires_explicit_test_factory_and_exact_content() -> None:
    policy = _bounded_policy()
    record = _test_policy_record(policy)
    accidentally_selected = SentinelPortfolioRiskPolicyVerifier(
        TestOnlySentinelPolicySource((record,))
    ).resolve(candidate=policy, evaluated_at=NOW)
    assert accidentally_selected.status is SentinelPolicyResolutionStatus.UNAVAILABLE

    verified = _test_policy_verifier((record,)).resolve(
        candidate=policy, evaluated_at=NOW
    )
    assert verified.status is SentinelPolicyResolutionStatus.TEST_ONLY_VALIDATED
    assert verified.record == record and verified.execution_authority is False
    assert verified.approval_readiness.approver_verified is True
    assert verified.approval_readiness.policy_fingerprint_verified is True
    assert verified.approval_readiness.production_policy_resolution_available is False

    altered = policy.model_copy(update={"max_order_notional": policy.max_order_notional + 1})
    rejected = _test_policy_verifier((record,)).resolve(
        candidate=altered, evaluated_at=NOW
    )
    assert rejected.status is SentinelPolicyResolutionStatus.REJECTED
    assert "SENTINEL_RISK_POLICY_CONTENT_MISMATCH" in rejected.reason_codes

    conflicting = _test_policy_record(
        policy.model_copy(update={"max_order_notional": policy.max_order_notional + 1})
    )
    with pytest.raises(ValueError, match="conflicting"):
        TestOnlySentinelPolicySource((record, conflicting))


def test_effective_start_is_inclusive_and_expiry_is_exclusive() -> None:
    policy = _bounded_policy()
    record = _test_policy_record(policy)
    verifier = _test_policy_verifier((record,))
    assert verifier.resolve(
        candidate=policy, evaluated_at=policy.effective_at
    ).status is SentinelPolicyResolutionStatus.TEST_ONLY_VALIDATED
    expired = verifier.resolve(candidate=policy, evaluated_at=policy.expires_at)
    assert expired.status is SentinelPolicyResolutionStatus.REJECTED
    assert "SENTINEL_RISK_POLICY_NOT_EFFECTIVE" in expired.reason_codes


@pytest.mark.parametrize(
    ("record_change", "at", "code"),
    [
        (
            {"lifecycle": SentinelPolicyLifecycle.REVOKED, "lifecycle_version": 2,
             "revoked_reason": "test", "lifecycle_changed_at": NOW},
            NOW,
            "SENTINEL_RISK_POLICY_REVOKED",
        ),
        (
            {"lifecycle": SentinelPolicyLifecycle.SUPERSEDED, "lifecycle_version": 2,
             "superseded_by_version": "v2", "lifecycle_changed_at": NOW},
            NOW,
            "SENTINEL_RISK_POLICY_SUPERSEDED",
        ),
        ({}, NOW + timedelta(minutes=10), "SENTINEL_RISK_POLICY_NOT_EFFECTIVE"),
        ({}, NOW - timedelta(minutes=1, seconds=1), "SENTINEL_RISK_POLICY_NOT_EFFECTIVE"),
    ],
)
def test_lifecycle_and_effective_boundaries_fail_closed(record_change, at, code: str) -> None:
    record = _test_policy_record(**record_change)
    result = _test_policy_verifier((record,)).resolve(
        candidate=record.policy, evaluated_at=at
    )
    assert result.status is SentinelPolicyResolutionStatus.REJECTED
    assert code in result.reason_codes


def test_local_catalog_is_durable_idempotent_and_non_authoritative(tmp_path: Path) -> None:
    path = tmp_path / "policy.sqlite3"
    record = _local_policy_record()
    first = LocalSentinelPolicyCatalog(path)
    assert first.register(record) == record
    assert first.register(record) == record
    reopened = LocalSentinelPolicyCatalog(path)
    assert reopened.resolve(
        policy_id=record.policy.policy_id, policy_version=record.policy.policy_version
    ) == record
    assert len(reopened.history(
        policy_id=record.policy.policy_id, policy_version=record.policy.policy_version
    )) == 1
    with pytest.raises(SentinelPolicyCatalogError, match="NON_LOCAL"):
        reopened.register(_test_policy_record(record.policy))


def test_local_catalog_detects_conflicts_and_persists_revocation(tmp_path: Path) -> None:
    path = tmp_path / "policy.sqlite3"
    catalog = LocalSentinelPolicyCatalog(path)
    record = _local_policy_record()
    catalog.register(record)
    changed = _local_policy_record(
        record.policy.model_copy(update={"max_order_notional": record.policy.max_order_notional + 1})
    )
    with pytest.raises(SentinelPolicyCatalogError, match="VERSION_CONFLICT"):
        catalog.register(changed)
    revoked = catalog.revoke(
        policy_id=record.policy.policy_id,
        policy_version=record.policy.policy_version,
        reason="operator-revoked",
        changed_at=NOW,
    )
    assert revoked.lifecycle is SentinelPolicyLifecycle.REVOKED
    assert LocalSentinelPolicyCatalog(path).resolve(
        policy_id=record.policy.policy_id, policy_version=record.policy.policy_version
    ) == revoked


def test_local_catalog_supersession_is_atomic_and_one_active_version(tmp_path: Path) -> None:
    catalog = LocalSentinelPolicyCatalog(tmp_path / "policy.sqlite3")
    current = _local_policy_record()
    catalog.register(current)
    replacement_policy = _bounded_policy(policy_version="V2")
    replacement = _local_policy_record(
        replacement_policy,
        supersedes_version=current.policy.policy_version,
        registered_at=NOW,
        lifecycle_changed_at=NOW,
    )
    ended, active = catalog.supersede(
        current_policy_id=current.policy.policy_id,
        current_policy_version=current.policy.policy_version,
        replacement=replacement,
        changed_at=NOW,
    )
    assert ended.lifecycle is SentinelPolicyLifecycle.SUPERSEDED
    assert active.lifecycle is SentinelPolicyLifecycle.ACTIVE
    conflicting = _local_policy_record(_bounded_policy(policy_version="V3"))
    with pytest.raises(SentinelPolicyCatalogError, match="ACTIVE_VERSION_CONFLICT"):
        catalog.register(conflicting)


def test_catalog_rejects_memory_and_tampered_content(tmp_path: Path) -> None:
    with pytest.raises(SentinelPolicyCatalogError, match="DURABLE_PATH"):
        LocalSentinelPolicyCatalog(":memory:")
    path = tmp_path / "policy.sqlite3"
    catalog = LocalSentinelPolicyCatalog(path)
    record = _local_policy_record()
    catalog.register(record)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sentinel_policy_records SET content_fingerprint = ?",
            ("0" * 64,),
        )
    with pytest.raises(SentinelPolicyCatalogError, match="TAMPERED"):
        catalog.resolve(policy_id=record.policy.policy_id, policy_version=record.policy.policy_version)


def test_catalog_rejects_partial_or_weakened_schema_without_repair(tmp_path: Path) -> None:
    partial = tmp_path / "partial.sqlite3"
    with sqlite3.connect(partial) as connection:
        connection.execute(
            "CREATE TABLE sentinel_policy_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sentinel_policy_metadata VALUES ('schema_version', '1')"
        )
    with pytest.raises(SentinelPolicyCatalogError, match="SCHEMA_INVALID"):
        LocalSentinelPolicyCatalog(partial)
    with sqlite3.connect(partial) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"sentinel_policy_metadata"}

    weakened = tmp_path / "weakened.sqlite3"
    LocalSentinelPolicyCatalog(weakened)
    with sqlite3.connect(weakened) as connection:
        connection.execute("DROP INDEX sentinel_policy_one_active_version")
    with pytest.raises(SentinelPolicyCatalogError, match="SCHEMA_INVALID"):
        LocalSentinelPolicyCatalog(weakened)


def test_separate_catalog_instances_cannot_create_two_active_versions(tmp_path: Path) -> None:
    path = tmp_path / "policy.sqlite3"
    first = LocalSentinelPolicyCatalog(path)
    second = LocalSentinelPolicyCatalog(path)
    first.register(_local_policy_record(_bounded_policy(policy_version="V1")))
    with pytest.raises(SentinelPolicyCatalogError, match="ACTIVE_VERSION_CONFLICT"):
        second.register(_local_policy_record(_bounded_policy(policy_version="V2")))
