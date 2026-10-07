from datetime import UTC, datetime
import sqlite3

import pytest

from app.audit.models import AuditEvent, AuditEventType
from app.audit.store import AuditStore, AuditStoreCapacity, AuditStoreConflict, AuditStoreUnavailable


def _event(**changes: object) -> AuditEvent:
    values: dict[str, object] = {
        "event_type": AuditEventType.EXECUTION_LIFECYCLE,
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "execution_id": "execution-1",
        "proposal_id": "proposal-1",
        "intent_id": "intent-1",
        "state": "ready_for_submission",
        "reason_code": "registered",
        "provenance_fingerprint": "a" * 64,
    }
    values.update(changes)
    return AuditEvent(**values)


def test_audit_persists_across_store_restart_and_is_immutable(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    event = _event()
    AuditStore(path).append(event)
    restored = AuditStore(path).events()
    assert restored == (event,)
    with pytest.raises(Exception):
        restored[0].state = "blocked"


def test_audit_duplicate_is_idempotent_and_conflict_fails_closed(tmp_path) -> None:
    store = AuditStore(tmp_path / "ledger.sqlite3")
    event = _event()
    assert store.append(event) == event
    assert store.append(event) == event
    with pytest.raises(AuditStoreConflict):
        store.append(_event(details=(("diagnostic", "different"),)))
    assert len(store.events()) == 1


def test_audit_capacity_and_corruption_fail_closed(tmp_path) -> None:
    store = AuditStore(tmp_path / "ledger.sqlite3", max_events=1)
    store.append(_event())
    with pytest.raises(AuditStoreCapacity):
        store.append(_event(execution_id="execution-2", intent_id="intent-2"))
    bad = tmp_path / "bad.sqlite3"
    bad.write_text("not-a-database", encoding="utf-8")
    with pytest.raises(AuditStoreUnavailable):
        AuditStore(bad)


def test_corrupted_persisted_event_fails_closed_on_read(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = AuditStore(path)
    store.append(_event())
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE audit_events SET details_json = ?", ("not-json",))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(AuditStoreUnavailable):
        store.events()
    assert not store.is_available()


def test_audit_rejects_sensitive_detail_keys(tmp_path) -> None:
    del tmp_path
    with pytest.raises(ValueError):
        _event(details=(("api_key", "not-allowed"),))
