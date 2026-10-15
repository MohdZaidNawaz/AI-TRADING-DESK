"""Durable local policy-approval lifecycle tests; no external authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import inspect
from pathlib import Path
import sqlite3

import pytest

from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalLifecycle,
    SentinelPolicyApprovalSourceStatus,
)
from app.services.sentinel_policy_approval import (
    NonAuthoritativeLifecycleApprovalVerifier,
    approval_binding_codes,
)
from app.services.sentinel_policy_approval_catalog import (
    LocalSentinelPolicyApprovalCatalog,
    SentinelPolicyApprovalCatalogError,
)
from app.services.sentinel_policy_governance import SentinelPolicyGovernanceBoundary
from tests.test_demo_execution_readiness import NOW
from tests.test_sentinel_policy_authority import (
    _bounded_policy,
    _test_approval_state,
)


def _catalog(path: Path) -> LocalSentinelPolicyApprovalCatalog:
    return LocalSentinelPolicyApprovalCatalog(path)


def test_recorded_claim_is_durable_idempotent_and_explicitly_non_authoritative(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approvals.sqlite3"
    policy = _bounded_policy()
    state = _test_approval_state(policy)
    catalog = _catalog(path)

    assert catalog.register(state, policy=policy) == state
    assert catalog.register(state, policy=policy) == state
    reopened = _catalog(path)
    result = reopened.resolve(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        artifact_id=state.artifact.approval_artifact_id,
        evaluated_at=NOW,
    )

    assert result.status is SentinelPolicyApprovalSourceStatus.AVAILABLE
    assert result.state == state
    assert catalog.source_id.startswith("local_non_authoritative_")
    assert len(reopened.history(artifact_id=state.artifact.approval_artifact_id)) == 1


def test_policy_binding_and_one_active_claim_are_enforced(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path / "approvals.sqlite3")
    policy = _bounded_policy()
    first = _test_approval_state(policy)
    catalog.register(first, policy=policy)

    conflicting = _test_approval_state(policy, proof="DIFFERENT-TEST-PROOF")
    with pytest.raises(SentinelPolicyApprovalCatalogError, match="ACTIVE_CONFLICT"):
        catalog.register(conflicting, policy=policy)

    altered_policy = policy.model_copy(
        update={"max_order_notional": policy.max_order_notional + 1}
    )
    with pytest.raises(
        SentinelPolicyApprovalCatalogError, match="FINGERPRINT_MISMATCH"
    ):
        _catalog(tmp_path / "other.sqlite3").register(
            first, policy=altered_policy
        )


def test_revocation_is_restart_safe_idempotent_and_conflict_checked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approvals.sqlite3"
    policy = _bounded_policy()
    state = _test_approval_state(policy)
    catalog = _catalog(path)
    catalog.register(state, policy=policy)

    revoked = catalog.revoke(
        artifact_id=state.artifact.approval_artifact_id,
        reason="governance-withdrawn",
        changed_at=NOW,
    )
    assert revoked.lifecycle is SentinelPolicyApprovalLifecycle.REVOKED
    assert _catalog(path).revoke(
        artifact_id=state.artifact.approval_artifact_id,
        reason="governance-withdrawn",
        changed_at=NOW + timedelta(seconds=1),
    ) == revoked
    with pytest.raises(SentinelPolicyApprovalCatalogError, match="REVOCATION_CONFLICT"):
        _catalog(path).revoke(
            artifact_id=state.artifact.approval_artifact_id,
            reason="different-reason",
            changed_at=NOW + timedelta(seconds=2),
        )

    resolved = _catalog(path).resolve(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        artifact_id=state.artifact.approval_artifact_id,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    assert resolved.state == revoked
    assert "SENTINEL_POLICY_APPROVAL_REVOKED" in approval_binding_codes(
        approval=revoked,
        policy=policy,
        evaluated_at=NOW + timedelta(seconds=3),
    )
    assert len(catalog.history(artifact_id=state.artifact.approval_artifact_id)) == 2


def test_supersession_is_atomic_and_old_artifact_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approvals.sqlite3"
    policy = _bounded_policy()
    current = _test_approval_state(policy)
    replacement = _test_approval_state(
        policy,
        proof="REPLACEMENT-TEST-PROOF",
        supersedes_approval_artifact_id=current.artifact.approval_artifact_id,
    )
    catalog = _catalog(path)
    catalog.register(current, policy=policy)

    ended, active = catalog.supersede(
        current_artifact_id=current.artifact.approval_artifact_id,
        replacement=replacement,
        policy=policy,
        changed_at=NOW,
    )
    assert ended.lifecycle is SentinelPolicyApprovalLifecycle.SUPERSEDED
    assert active == replacement
    assert "SENTINEL_POLICY_APPROVAL_SUPERSEDED" in approval_binding_codes(
        approval=ended, policy=policy, evaluated_at=NOW
    )
    reopened = _catalog(path)
    assert reopened.resolve(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        artifact_id=replacement.artifact.approval_artifact_id,
        evaluated_at=NOW,
    ).state == replacement


def test_expired_claim_remains_history_not_current_authority(tmp_path: Path) -> None:
    policy = _bounded_policy()
    state = _test_approval_state(policy)
    catalog = _catalog(tmp_path / "approvals.sqlite3")
    catalog.register(state, policy=policy)

    resolved = catalog.resolve(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        artifact_id=state.artifact.approval_artifact_id,
        evaluated_at=state.artifact.expires_at + timedelta(seconds=1),
    )
    assert resolved.status is SentinelPolicyApprovalSourceStatus.AVAILABLE
    assert "SENTINEL_POLICY_APPROVAL_NOT_EFFECTIVE" in approval_binding_codes(
        approval=resolved.state,  # type: ignore[arg-type]
        policy=policy,
        evaluated_at=state.artifact.expires_at + timedelta(seconds=1),
    )


def test_commit_failure_rolls_back_without_false_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "approvals.sqlite3"
    policy = _bounded_policy()
    state = _test_approval_state(policy)
    catalog = _catalog(path)

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(catalog, "_commit", fail_commit)
    with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
        catalog.register(state, policy=policy)
    missing = _catalog(path).resolve(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        artifact_id=state.artifact.approval_artifact_id,
        evaluated_at=NOW,
    )
    assert missing.status is SentinelPolicyApprovalSourceStatus.NOT_FOUND


def test_concurrent_instances_cannot_record_conflicting_active_claims(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approvals.sqlite3"
    policy = _bounded_policy()
    states = (
        _test_approval_state(policy, proof="CONCURRENT-PROOF-A"),
        _test_approval_state(policy, proof="CONCURRENT-PROOF-B"),
    )
    _catalog(path)

    def register(state):
        try:
            _catalog(path).register(state, policy=policy)
            return "created"
        except SentinelPolicyApprovalCatalogError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(register, states))
    assert results.count("created") == 1
    assert sum("ACTIVE_CONFLICT" in result for result in results) == 1


def test_weakened_or_partial_schema_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    partial = tmp_path / "partial.sqlite3"
    with sqlite3.connect(partial) as connection:
        connection.execute(
            "CREATE TABLE sentinel_policy_approval_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sentinel_policy_approval_metadata VALUES "
            "('schema_version', '1')"
        )
    with pytest.raises(SentinelPolicyApprovalCatalogError, match="SCHEMA_INVALID"):
        _catalog(partial)
    with sqlite3.connect(partial) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == {"sentinel_policy_approval_metadata"}

    weakened = tmp_path / "weakened.sqlite3"
    _catalog(weakened)
    with sqlite3.connect(weakened) as connection:
        connection.execute("DROP INDEX sentinel_policy_approval_one_active")
    with pytest.raises(SentinelPolicyApprovalCatalogError, match="SCHEMA_INVALID"):
        _catalog(weakened)


def test_local_lifecycle_source_cannot_masquerade_as_policy_authority(
    tmp_path: Path,
) -> None:
    policy = _bounded_policy()
    state = _test_approval_state(policy)
    catalog = _catalog(tmp_path / "approvals.sqlite3")
    catalog.register(state, policy=policy)

    result = SentinelPolicyGovernanceBoundary(approval_source=catalog).assess(
        approval=state,
        policy=policy,
        policy_source_configured=True,
        evaluated_at=NOW,
    )
    assert "SENTINEL_POLICY_APPROVAL_SOURCE_UNTRUSTED" in result.reason_codes
    assert result.readiness.production_policy_resolution_available is False

    observed = NonAuthoritativeLifecycleApprovalVerifier(catalog).verify(
        approval=state,
        policy=policy,
        policy_source_configured=True,
        evaluated_at=NOW,
    )
    assert observed.status.value == "unavailable"
    assert observed.readiness.approval_source_configured is True
    assert observed.readiness.revocation_status_available is True
    assert observed.readiness.production_policy_resolution_available is False
    assert "SENTINEL_POLICY_APPROVAL_SOURCE_NON_AUTHORITATIVE" in observed.reason_codes


def test_observed_revocation_blocks_and_arbitrary_source_cannot_authorize(
    tmp_path: Path,
) -> None:
    policy = _bounded_policy()
    state = _test_approval_state(policy)
    catalog = _catalog(tmp_path / "approvals.sqlite3")
    catalog.register(state, policy=policy)
    revoked = catalog.revoke(
        artifact_id=state.artifact.approval_artifact_id,
        reason="governance-withdrawn",
        changed_at=NOW,
    )

    observed = NonAuthoritativeLifecycleApprovalVerifier(catalog).verify(
        approval=revoked,
        policy=policy,
        policy_source_configured=True,
        evaluated_at=NOW + timedelta(seconds=1),
    )
    assert observed.status.value == "unavailable"
    assert "SENTINEL_POLICY_APPROVAL_REVOKED" in observed.reason_codes

    class FabricatedSource:
        source_id = "caller-fabricated"

        def resolve(self, **_kwargs):
            return catalog.resolve(
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                artifact_id=state.artifact.approval_artifact_id,
                evaluated_at=NOW,
            )

    fabricated = NonAuthoritativeLifecycleApprovalVerifier(FabricatedSource()).verify(
        approval=revoked,
        policy=policy,
        policy_source_configured=True,
        evaluated_at=NOW + timedelta(seconds=1),
    )
    assert fabricated.status.value == "unavailable"
    assert fabricated.readiness.production_policy_resolution_available is False


def test_catalog_has_no_execution_or_provider_dependencies() -> None:
    import app.services.sentinel_policy_approval_catalog as module

    source = inspect.getsource(module)
    for forbidden in (
        "MetaTrader5",
        "order_send",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "MT5ExecutionAdapter",
        ".submit(",
    ):
        assert forbidden not in source
