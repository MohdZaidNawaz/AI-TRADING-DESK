"""Phase 13A durable authority-state tests with synthetic inputs only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import inspect
import sqlite3

import pytest

from app.audit.service import AuditService
from app.audit.store import AuditStore, AuditStoreError
from app.execution.durable_authority_store import (
    DurableAuthorityConflict,
    DurableAuthorityStateError,
    DurableAuthorityStore,
    DurableAuthorityUnavailable,
)
from app.models.durable_authority import DurableAuthorityStatus
from tests.test_demo_execution_authorization import _authorize
from tests.test_demo_execution_readiness import NOW


def _records(tmp_path):
    _service, _memory_store, _registry, _audit, _state, values, result = _authorize(tmp_path / "inputs")
    assert result.authorization is not None
    return values["decision"], result.authorization


def _store(tmp_path) -> DurableAuthorityStore:
    return DurableAuthorityStore(tmp_path / "durable-authority.sqlite3")


def test_durable_registration_verification_and_idempotency(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store = _store(tmp_path)
    approval, created = store.register_approval(decision, now=NOW)
    demo, issued = store.issue_demo_authorization(authorization, now=NOW)
    assert created is True and issued is True
    assert store.verify_approval(decision, now=NOW) == approval
    assert store.verify_demo_authorization(authorization, now=NOW) == demo
    assert store.register_approval(decision, now=NOW)[1] is False
    assert store.issue_demo_authorization(authorization, now=NOW)[1] is False
    assert store.history(kind="approval", external_id=approval.approval_id) == (
        (1, None, "active", "durable_approval_registered"),
    )


def test_conflicting_or_mismatched_lineage_fails_closed(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store = _store(tmp_path)
    store.register_approval(decision, now=NOW)
    store.issue_demo_authorization(authorization, now=NOW)
    conflicting = decision.model_copy(update={"analysis_id": "other"})
    with pytest.raises(DurableAuthorityConflict):
        store.register_approval(conflicting, now=NOW)
    with pytest.raises(DurableAuthorityStateError, match="LINEAGE_MISMATCH"):
        store.verify_approval(conflicting, now=NOW)
    # The authorization ID binds analysis lineage, so changing it yields a
    # different external identity.  Change a non-identity source field to
    # prove that verification never accepts mismatched stored lineage.
    conflicting_auth = authorization.model_copy(update={"status": "revoked"})
    with pytest.raises(DurableAuthorityStateError, match="LINEAGE_MISMATCH"):
        store.verify_demo_authorization(conflicting_auth, now=NOW)


def test_revoke_expiry_and_reconciliation_persist_after_reopen(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store = _store(tmp_path)
    approval, _ = store.register_approval(decision, now=NOW)
    demo, _ = store.issue_demo_authorization(authorization, now=NOW)
    revoked = store.revoke_approval(approval.approval_id, now=NOW + timedelta(seconds=1))
    required = store.mark_reconciliation_required(
        kind="demo_authorization", external_id=demo.authorization_id, now=NOW + timedelta(seconds=1)
    )
    reopened = DurableAuthorityStore(store.path)
    assert reopened.get_approval(approval.approval_id) == revoked
    assert reopened.get_demo_authorization(demo.authorization_id) == required
    with pytest.raises(DurableAuthorityStateError, match="REVOKED"):
        reopened.verify_approval(decision, now=NOW + timedelta(seconds=2))
    with pytest.raises(DurableAuthorityStateError, match="RECONCILIATION_REQUIRED"):
        reopened.verify_demo_authorization(authorization, now=NOW + timedelta(seconds=2))


def test_expiry_is_persisted_not_merely_reported(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store = _store(tmp_path)
    approval, _ = store.register_approval(decision, now=NOW)
    demo, _ = store.issue_demo_authorization(authorization, now=NOW)
    with pytest.raises(DurableAuthorityStateError, match="EXPIRED"):
        store.verify_approval(decision, now=decision.expires_at)
    with pytest.raises(DurableAuthorityStateError, match="EXPIRED"):
        store.verify_demo_authorization(authorization, now=authorization.expires_at)
    assert store.get_approval(approval.approval_id).status is DurableAuthorityStatus.EXPIRED
    assert store.get_demo_authorization(demo.authorization_id).status is DurableAuthorityStatus.EXPIRED


def test_competing_consumption_is_serialized_across_store_instances(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    path = tmp_path / "shared.sqlite3"
    seed = DurableAuthorityStore(path)
    seed.register_approval(decision, now=NOW)
    seed.issue_demo_authorization(authorization, now=NOW)

    def consume() -> bool:
        current = DurableAuthorityStore(path)
        demo, demo_consumed = current.consume_demo_authorization(authorization, now=NOW + timedelta(seconds=1))
        assert demo.status is DurableAuthorityStatus.CONSUMED
        return demo_consumed

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: consume(), range(2)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1
    approval, first = seed.consume_approval(decision, now=NOW + timedelta(seconds=1))
    repeated, second = seed.consume_approval(decision, now=NOW + timedelta(seconds=1))
    assert approval.status is repeated.status is DurableAuthorityStatus.CONSUMED
    assert first is True and second is False


def test_invalid_terminal_transitions_and_commit_failure_fail_closed(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store = _store(tmp_path)
    approval, _ = store.register_approval(decision, now=NOW)
    store.revoke_approval(approval.approval_id, now=NOW + timedelta(seconds=1))
    with pytest.raises(DurableAuthorityStateError, match="REVOKED"):
        store.consume_approval(decision, now=NOW + timedelta(seconds=2))
    failing = _store(tmp_path / "failing")
    failing._fail_next_commit_for_test = True
    with pytest.raises(DurableAuthorityUnavailable):
        failing.issue_demo_authorization(authorization, now=NOW)
    # A commit fault permanently closes this instance.  Reopening proves the
    # transaction was rolled back rather than exposing an in-memory fallback.
    reopened = DurableAuthorityStore(failing.path)
    assert reopened.get_demo_authorization(authorization.authorization_id) is None


def test_outbox_replay_is_idempotent_and_audit_failure_blocks_projection(tmp_path) -> None:
    decision, authorization = _records(tmp_path)
    store = _store(tmp_path)
    store.register_approval(decision, now=NOW)
    store.issue_demo_authorization(authorization, now=NOW)
    audit = AuditService(AuditStore(tmp_path / "audit.sqlite3"))
    assert store.project_outbox(audit) == 2
    assert store.project_outbox(audit) == 0

    class _FailingAudit:
        def record(self, _event):
            raise AuditStoreError("synthetic")

    store.revoke_demo_authorization(authorization.authorization_id, now=NOW + timedelta(seconds=1))
    with pytest.raises(DurableAuthorityUnavailable, match="audit projection"):
        store.project_outbox(_FailingAudit())
    assert store.project_outbox(audit) == 1


def test_partial_outbox_projection_replays_idempotently_after_reopen(tmp_path) -> None:
    decision, _authorization = _records(tmp_path)
    store = _store(tmp_path)
    store.register_approval(decision, now=NOW)
    audit = AuditService(AuditStore(tmp_path / "audit.sqlite3"))

    # The external audit write succeeds, but acknowledgement in the durable
    # store fails.  Restart/replay must acknowledge the same deterministic
    # event rather than duplicate it or treat it as an authorized transition.
    store._fail_next_commit_for_test = True
    with pytest.raises(DurableAuthorityUnavailable, match="outbox acknowledgement"):
        store.project_outbox(audit)
    assert len(audit.events()) == 1

    reopened = DurableAuthorityStore(store.path)
    assert reopened.project_outbox(audit) == 1
    assert len(audit.events()) == 1
    assert reopened.project_outbox(audit) == 0


def test_corrupt_missing_and_unsupported_schema_fail_closed(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not a sqlite database", encoding="utf-8")
    with pytest.raises(DurableAuthorityUnavailable):
        DurableAuthorityStore(corrupt)
    missing = tmp_path / "missing.sqlite3"
    connection = sqlite3.connect(missing)
    connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.commit(); connection.close()
    with pytest.raises(DurableAuthorityUnavailable):
        DurableAuthorityStore(missing)
    store = _store(tmp_path / "version")
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE durable_authority_schema_metadata SET value = 'other' WHERE key = 'schema_version'")
    connection.commit(); connection.close()
    with pytest.raises(DurableAuthorityUnavailable, match="unsupported"):
        DurableAuthorityStore(store.path)


def test_reopen_never_recreates_missing_authority(tmp_path) -> None:
    decision, _authorization = _records(tmp_path)
    store = _store(tmp_path)
    store.register_approval(decision, now=NOW)
    reopened = DurableAuthorityStore(store.path)
    missing = decision.model_copy(update={"approval_id": "missing-approved-decision"})
    with pytest.raises(DurableAuthorityStateError, match="NOT_FOUND"):
        reopened.verify_approval(missing, now=NOW)
    assert reopened.recover(now=NOW) == ()


def test_phase_13a_has_no_submission_or_broker_path() -> None:
    import app.execution.durable_authority_store as module

    source = inspect.getsource(module)
    for forbidden in ("order_send", "MT5ExecutionAdapter", ".submit(", "from app.execution.pilot", "from app.services.execution_gate"):
        assert forbidden not in source
