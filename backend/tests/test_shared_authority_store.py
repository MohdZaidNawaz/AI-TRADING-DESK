"""Phase 13C-1 shared-authority bootstrap tests; no execution dependencies."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import inspect
import sqlite3

import pytest

from app.execution.shared_authority_store import (
    SharedAuthorityProjectionError,
    SharedAuthorityStore,
    SharedAuthorityUnavailable,
)
from app.models.shared_authority import (
    SharedAuthorityBootstrapEvent,
    SharedAuthorityMode,
)


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
HISTORICAL_V1_CHECKSUM = "05087daba15f6689cf92d668a9820bf8533d2f3d7fa77dfc3d31db5050f407e6"
HISTORICAL_V1_SCHEMA = """
CREATE TABLE shared_authority_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE shared_authority_schema_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE shared_authority_cutover_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    state TEXT NOT NULL CHECK (state = 'bootstrap_non_authoritative'),
    updated_at TEXT NOT NULL
);
CREATE TABLE shared_authority_entities (
    entity_id TEXT PRIMARY KEY,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN (
        'schema_bootstrap', 'sentinel_approval', 'demo_authorization',
        'execution_reservation'
    )),
    status TEXT NOT NULL,
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    semantic_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE shared_sentinel_approvals (
    entity_id TEXT PRIMARY KEY REFERENCES shared_authority_entities(entity_id) ON DELETE RESTRICT,
    approval_id TEXT NOT NULL UNIQUE, decision_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL, proposal_fingerprint TEXT NOT NULL,
    analysis_id TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
    policy_id TEXT NOT NULL, policy_fingerprint TEXT NOT NULL,
    assessment_id TEXT NOT NULL, assessment_fingerprint TEXT NOT NULL,
    account_id TEXT, expires_at TEXT NOT NULL
);
CREATE TABLE shared_demo_authorizations (
    entity_id TEXT PRIMARY KEY REFERENCES shared_authority_entities(entity_id) ON DELETE RESTRICT,
    authorization_id TEXT NOT NULL UNIQUE,
    approval_entity_id TEXT NOT NULL REFERENCES shared_sentinel_approvals(entity_id) ON DELETE RESTRICT,
    execution_intent_id TEXT NOT NULL, execution_intent_fingerprint TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
    policy_id TEXT NOT NULL, policy_fingerprint TEXT NOT NULL,
    account_id TEXT NOT NULL, account_reference TEXT NOT NULL,
    broker_server TEXT NOT NULL, canonical_symbol TEXT NOT NULL,
    broker_symbol TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE shared_execution_reservations (
    entity_id TEXT PRIMARY KEY REFERENCES shared_authority_entities(entity_id) ON DELETE RESTRICT,
    reservation_id TEXT NOT NULL UNIQUE, execution_identity TEXT NOT NULL UNIQUE,
    approval_entity_id TEXT NOT NULL REFERENCES shared_sentinel_approvals(entity_id) ON DELETE RESTRICT,
    authorization_entity_id TEXT NOT NULL REFERENCES shared_demo_authorizations(entity_id) ON DELETE RESTRICT,
    safety_evidence_fingerprint TEXT NOT NULL, proposal_fingerprint TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL, execution_intent_fingerprint TEXT NOT NULL,
    account_id TEXT NOT NULL, account_reference TEXT NOT NULL,
    broker_server TEXT NOT NULL, canonical_symbol TEXT NOT NULL,
    broker_symbol TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE shared_authority_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id) ON DELETE RESTRICT,
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    from_status TEXT, to_status TEXT NOT NULL, event_state TEXT NOT NULL,
    reason_code TEXT NOT NULL, occurred_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
    UNIQUE(entity_id, lifecycle_version)
);
CREATE TABLE shared_authority_outbox (
    outbox_id TEXT PRIMARY KEY,
    lifecycle_event_id TEXT NOT NULL UNIQUE REFERENCES shared_authority_lifecycle_events(event_id) ON DELETE RESTRICT,
    entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id) ON DELETE RESTRICT,
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    event_state TEXT NOT NULL, reason_code TEXT NOT NULL,
    occurred_at TEXT NOT NULL, fingerprint TEXT NOT NULL, delivered_at TEXT
);
CREATE INDEX shared_authority_lifecycle_entity_version_index
    ON shared_authority_lifecycle_events (entity_id, lifecycle_version);
CREATE INDEX shared_authority_outbox_pending_index
    ON shared_authority_outbox (delivered_at, lifecycle_version, outbox_id);
"""


def _historical_v1_database(
    path,
    *,
    schema: str = HISTORICAL_V1_SCHEMA,
    checksum: str = HISTORICAL_V1_CHECKSUM,
    migration_name: str = "shared_authority_bootstrap",
    migration_state: str = "READY",
) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema)
        connection.execute(
            "INSERT INTO shared_authority_schema_metadata (key, value) VALUES "
            "('schema_version', '1'), ('migration_state', ?)",
            (migration_state,),
        )
        connection.execute(
            "INSERT INTO shared_authority_schema_migrations "
            "(version, name, checksum, applied_at) VALUES (1, ?, ?, ?)",
            (migration_name, checksum, NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO shared_authority_cutover_state (singleton_id, state, updated_at) "
            "VALUES (1, 'bootstrap_non_authoritative', ?)",
            (NOW.isoformat(),),
        )
    finally:
        connection.close()


def _event(*, offset: int = 0, state: str = "bootstrap_validated") -> SharedAuthorityBootstrapEvent:
    return SharedAuthorityBootstrapEvent(
        event_state=state,
        occurred_at=NOW + timedelta(seconds=offset),
        reason_code="SCHEMA_BOOTSTRAP_VALIDATED",
        details=(("scope", "non_authoritative"),),
    )


def test_fresh_and_idempotent_initialization_is_sentinel_approval_owner(tmp_path) -> None:
    path = tmp_path / "shared-authority.sqlite3"
    store = SharedAuthorityStore(path)
    diagnostics = store.diagnostics()
    assert diagnostics.available is True
    assert diagnostics.mode is SharedAuthorityMode.SENTINEL_APPROVAL_OWNER
    assert diagnostics.schema_version == "2"
    assert diagnostics.migration_state == "READY"
    assert store.foreign_keys_enabled() is True

    reopened = SharedAuthorityStore(path)
    assert reopened.diagnostics() == diagnostics
    assert reopened.lifecycle_events() == ()


def test_schema_reserves_normalized_future_authority_tables_and_foreign_keys(tmp_path) -> None:
    store = SharedAuthorityStore(tmp_path / "shared-authority.sqlite3")
    connection = sqlite3.connect(store.path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "shared_sentinel_approvals",
            "shared_demo_authorizations",
            "shared_execution_reservations",
            "shared_authority_lifecycle_events",
            "shared_authority_outbox",
        }.issubset(tables)
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO shared_demo_authorizations "
                "(entity_id, authorization_id, approval_entity_id, execution_intent_id, "
                "execution_intent_fingerprint, proposal_fingerprint, evidence_fingerprint, "
                "policy_id, policy_fingerprint, account_id, account_reference, broker_server, "
                "canonical_symbol, broker_symbol, expires_at) "
                "VALUES ('missing', 'auth', 'missing-approval', 'intent', 'f', 'p', 'e', "
                "'policy', 'pf', 'account', 'reference', 'server', 'XAUUSD', 'XAUUSD', 'future')"
            )
    finally:
        connection.close()


def test_valid_v1_bootstrap_migrates_atomically_to_sentinel_approval_ownership(tmp_path) -> None:
    import app.execution.shared_authority_store as module

    path = tmp_path / "v1-bootstrap.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        module._create_canonical_schema(connection, schema_version="1")
        connection.execute(
            "INSERT INTO shared_authority_schema_metadata (key, value) VALUES "
            "('schema_version', '1'), ('migration_state', 'READY')"
        )
        connection.execute(
            "INSERT INTO shared_authority_schema_migrations "
            "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (1, "shared_authority_bootstrap", module._migration_checksum(1), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO shared_authority_cutover_state (singleton_id, state, updated_at) "
            "VALUES (1, 'bootstrap_non_authoritative', ?)",
            (NOW.isoformat(),),
        )
    finally:
        connection.close()

    migrated = SharedAuthorityStore(path)
    assert migrated.diagnostics().schema_version == "2"
    assert migrated.mode is SharedAuthorityMode.SENTINEL_APPROVAL_OWNER
    with sqlite3.connect(path) as verified:
        assert verified.execute(
            "SELECT state FROM shared_authority_cutover_state WHERE singleton_id = 1"
        ).fetchone() == ("sentinel_approval_owner",)
        assert verified.execute(
            "SELECT name FROM shared_authority_schema_migrations WHERE version = 2"
        ).fetchone() == ("sentinel_approval_ownership",)


def test_exact_historical_v1_migrates_and_preserves_lifecycle_and_outbox(tmp_path) -> None:
    path = tmp_path / "historical-v1.sqlite3"
    _historical_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO shared_authority_entities "
            "(entity_id, entity_kind, status, lifecycle_version, semantic_fingerprint, created_at, updated_at) "
            "VALUES (?, 'schema_bootstrap', 'bootstrap', 1, ?, ?, ?)",
            ("historical-bootstrap", "a" * 64, NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO shared_authority_lifecycle_events "
            "(event_id, entity_id, lifecycle_version, from_status, to_status, event_state, "
            "reason_code, occurred_at, fingerprint) VALUES (?, ?, 1, 'bootstrap', 'bootstrap', ?, ?, ?, ?)",
            (
                "historical-event",
                "historical-bootstrap",
                "bootstrap_validated",
                "SCHEMA_BOOTSTRAP_VALIDATED",
                NOW.isoformat(),
                "b" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO shared_authority_outbox "
            "(outbox_id, lifecycle_event_id, entity_id, lifecycle_version, event_state, "
            "reason_code, occurred_at, fingerprint, delivered_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL)",
            (
                "historical-outbox",
                "historical-event",
                "historical-bootstrap",
                "bootstrap_validated",
                "SCHEMA_BOOTSTRAP_VALIDATED",
                NOW.isoformat(),
                "c" * 64,
            ),
        )

    migrated = SharedAuthorityStore(path)
    assert migrated.diagnostics().schema_version == "2"
    assert migrated.mode is SharedAuthorityMode.SENTINEL_APPROVAL_OWNER
    with sqlite3.connect(path) as verified:
        verified.execute("PRAGMA foreign_keys = ON")
        assert verified.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert verified.execute("PRAGMA foreign_key_check").fetchone() is None
        assert verified.execute(
            "SELECT checksum FROM shared_authority_schema_migrations WHERE version = 1"
        ).fetchone() == (HISTORICAL_V1_CHECKSUM,)
        assert verified.execute(
            "SELECT name FROM shared_authority_schema_migrations WHERE version = 2"
        ).fetchone() == ("sentinel_approval_ownership",)
        assert verified.execute(
            "SELECT event_id FROM shared_authority_lifecycle_events WHERE event_id = 'historical-event'"
        ).fetchone() == ("historical-event",)
        assert verified.execute(
            "SELECT outbox_id FROM shared_authority_outbox WHERE outbox_id = 'historical-outbox'"
        ).fetchone() == ("historical-outbox",)

    reopened = SharedAuthorityStore(path)
    assert reopened.diagnostics() == migrated.diagnostics()


def test_historical_v1_migration_commit_failure_rolls_back_exactly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "historical-v1-failed.sqlite3"
    _historical_v1_database(path)

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("injected historical migration commit failure")

    monkeypatch.setattr(SharedAuthorityStore, "_commit", staticmethod(fail_commit))
    with pytest.raises(SharedAuthorityUnavailable):
        SharedAuthorityStore(path)

    with sqlite3.connect(path) as verified:
        assert dict(verified.execute(
            "SELECT key, value FROM shared_authority_schema_metadata"
        ).fetchall()) == {"schema_version": "1", "migration_state": "READY"}
        assert verified.execute(
            "SELECT name, checksum FROM shared_authority_schema_migrations ORDER BY version"
        ).fetchall() == [("shared_authority_bootstrap", HISTORICAL_V1_CHECKSUM)]
        assert verified.execute(
            "SELECT state FROM shared_authority_cutover_state WHERE singleton_id = 1"
        ).fetchone() == ("bootstrap_non_authoritative",)


@pytest.mark.parametrize(
    ("changes", "match"),
    (
        ({"checksum": "0" * 64}, "migration history"),
        ({"migration_name": "unexpected_bootstrap"}, "migration history"),
        ({"migration_state": "MIGRATING"}, "migration is incomplete"),
        (
            {
                "schema": HISTORICAL_V1_SCHEMA.replace(
                    "state = 'bootstrap_non_authoritative'",
                    "state IN ('bootstrap_non_authoritative')",
                )
            },
            "schema structure is incompatible",
        ),
    ),
)
def test_unknown_or_mismatched_historical_v1_variants_fail_closed(
    tmp_path, changes: dict[str, str], match: str
) -> None:
    path = tmp_path / "invalid-historical-v1.sqlite3"
    _historical_v1_database(path, **changes)
    with pytest.raises(SharedAuthorityUnavailable, match=match):
        SharedAuthorityStore(path)


def test_historical_v1_foreign_key_violation_fails_closed(tmp_path) -> None:
    path = tmp_path / "historical-v1-invalid-foreign-key.sqlite3"
    _historical_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO shared_authority_lifecycle_events "
            "(event_id, entity_id, lifecycle_version, from_status, to_status, event_state, "
            "reason_code, occurred_at, fingerprint) VALUES "
            "('orphan', 'missing', 1, NULL, 'bootstrap', 'invalid', 'INVALID', ?, ?)",
            (NOW.isoformat(), "d" * 64),
        )

    with pytest.raises(SharedAuthorityUnavailable, match="foreign-key integrity"):
        SharedAuthorityStore(path)


def test_historical_v1_failed_integrity_check_fails_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.execution.shared_authority_store as module

    path = tmp_path / "historical-v1-invalid-integrity.sqlite3"
    _historical_v1_database(path)
    real_connect = sqlite3.connect

    class IntegrityFailureConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):  # type: ignore[no-untyped-def]
            if sql.strip().upper() == "PRAGMA INTEGRITY_CHECK":
                return super().execute("SELECT 'injected integrity failure'")
            return super().execute(sql, parameters)

    def connect_with_integrity_failure(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["factory"] = IntegrityFailureConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect_with_integrity_failure)
    with pytest.raises(SharedAuthorityUnavailable, match="integrity check failed"):
        SharedAuthorityStore(path)


def test_v1_to_v2_migration_commit_failure_leaves_v1_unmodified(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.execution.shared_authority_store as module

    path = tmp_path / "failed-v1-migration.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        module._create_canonical_schema(connection, schema_version="1")
        connection.execute(
            "INSERT INTO shared_authority_schema_metadata (key, value) VALUES "
            "('schema_version', '1'), ('migration_state', 'READY')"
        )
        connection.execute(
            "INSERT INTO shared_authority_schema_migrations "
            "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (1, "shared_authority_bootstrap", module._migration_checksum(1), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO shared_authority_cutover_state (singleton_id, state, updated_at) "
            "VALUES (1, 'bootstrap_non_authoritative', ?)",
            (NOW.isoformat(),),
        )
    finally:
        connection.close()

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("injected migration commit failure")

    monkeypatch.setattr(SharedAuthorityStore, "_commit", staticmethod(fail_commit))
    with pytest.raises(SharedAuthorityUnavailable):
        SharedAuthorityStore(path)
    with sqlite3.connect(path) as verified:
        assert verified.execute(
            "SELECT value FROM shared_authority_schema_metadata WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
        assert verified.execute(
            "SELECT state FROM shared_authority_cutover_state WHERE singleton_id = 1"
        ).fetchone() == ("bootstrap_non_authoritative",)


@pytest.mark.parametrize("kind", ("corrupt", "partial", "unsupported", "migrating"))
def test_invalid_or_incomplete_schema_fails_closed(tmp_path, kind: str) -> None:
    path = tmp_path / f"{kind}.sqlite3"
    if kind == "corrupt":
        path.write_text("not a sqlite database", encoding="utf-8")
    elif kind == "partial":
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit(); connection.close()
    else:
        seed = SharedAuthorityStore(path)
        connection = sqlite3.connect(seed.path)
        if kind == "unsupported":
            connection.execute(
                "UPDATE shared_authority_schema_metadata SET value = '999' "
                "WHERE key = 'schema_version'"
            )
        else:
            connection.execute(
                "UPDATE shared_authority_schema_metadata SET value = 'MIGRATING' "
                "WHERE key = 'migration_state'"
            )
        connection.commit(); connection.close()

    with pytest.raises(SharedAuthorityUnavailable):
        SharedAuthorityStore(path)


_OUTBOX_DEFINITIONS = {
    "missing_column": """CREATE TABLE shared_authority_outbox (
        outbox_id TEXT PRIMARY KEY,
        lifecycle_event_id TEXT NOT NULL UNIQUE
            REFERENCES shared_authority_lifecycle_events(event_id) ON DELETE RESTRICT,
        entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id)
            ON DELETE RESTRICT,
        lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
        event_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        delivered_at TEXT
    )""",
    "wrong_column_type": """CREATE TABLE shared_authority_outbox (
        outbox_id TEXT PRIMARY KEY,
        lifecycle_event_id TEXT NOT NULL UNIQUE
            REFERENCES shared_authority_lifecycle_events(event_id) ON DELETE RESTRICT,
        entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id)
            ON DELETE RESTRICT,
        lifecycle_version TEXT NOT NULL CHECK (lifecycle_version >= 1),
        event_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        delivered_at TEXT
    )""",
    "missing_foreign_keys": """CREATE TABLE shared_authority_outbox (
        outbox_id TEXT PRIMARY KEY,
        lifecycle_event_id TEXT NOT NULL UNIQUE,
        entity_id TEXT NOT NULL,
        lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
        event_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        delivered_at TEXT
    )""",
    "missing_unique_constraint": """CREATE TABLE shared_authority_outbox (
        outbox_id TEXT PRIMARY KEY,
        lifecycle_event_id TEXT NOT NULL
            REFERENCES shared_authority_lifecycle_events(event_id) ON DELETE RESTRICT,
        entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id)
            ON DELETE RESTRICT,
        lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
        event_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        delivered_at TEXT
    )""",
    "missing_check_constraint": """CREATE TABLE shared_authority_outbox (
        outbox_id TEXT PRIMARY KEY,
        lifecycle_event_id TEXT NOT NULL UNIQUE
            REFERENCES shared_authority_lifecycle_events(event_id) ON DELETE RESTRICT,
        entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id)
            ON DELETE RESTRICT,
        lifecycle_version INTEGER NOT NULL,
        event_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        delivered_at TEXT
    )""",
}


def _replace_outbox_schema(path, definition: str) -> str:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE shared_authority_outbox")
        connection.execute(definition)
        connection.execute(
            "CREATE INDEX shared_authority_outbox_pending_index "
            "ON shared_authority_outbox (delivered_at, lifecycle_version, outbox_id)"
        )
        connection.commit()
        return connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'shared_authority_outbox'"
        ).fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize("definition", tuple(_OUTBOX_DEFINITIONS.values()), ids=_OUTBOX_DEFINITIONS)
def test_metadata_cannot_make_weakened_schema_ready(tmp_path, definition: str) -> None:
    path = tmp_path / "weakened.sqlite3"
    SharedAuthorityStore(path)
    malformed_definition = _replace_outbox_schema(path, definition)

    with pytest.raises(SharedAuthorityUnavailable, match="schema structure"):
        SharedAuthorityStore(path)

    connection = sqlite3.connect(path)
    try:
        current = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'shared_authority_outbox'"
        ).fetchone()[0]
        assert current == malformed_definition
    finally:
        connection.close()


def test_missing_required_named_index_fails_closed_without_repair(tmp_path) -> None:
    path = tmp_path / "missing-index.sqlite3"
    SharedAuthorityStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX shared_authority_outbox_pending_index")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SharedAuthorityUnavailable, match="required indexes"):
        SharedAuthorityStore(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'shared_authority_outbox_pending_index'"
        ).fetchone() is None
    finally:
        connection.close()


def test_bootstrap_commit_failure_leaves_no_partial_schema(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bootstrap-commit-failure.sqlite3"

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("synthetic bootstrap commit failure")

    monkeypatch.setattr(SharedAuthorityStore, "_commit", staticmethod(fail_commit))
    with pytest.raises(SharedAuthorityUnavailable):
        SharedAuthorityStore(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall() == []
    finally:
        connection.close()


def test_bootstrap_lifecycle_and_outbox_are_atomic_and_idempotent(tmp_path) -> None:
    store = SharedAuthorityStore(tmp_path / "shared-authority.sqlite3")
    event = _event()
    stored, created = store.record_bootstrap_lifecycle(event)
    repeat, repeated = store.record_bootstrap_lifecycle(event)
    assert created is True and repeated is False
    assert repeat == stored
    assert store.lifecycle_events() == (stored,)
    assert store.pending_outbox() == (stored,)


def test_commit_failure_rolls_back_without_in_memory_fallback(tmp_path, monkeypatch) -> None:
    path = tmp_path / "shared-authority.sqlite3"
    store = SharedAuthorityStore(path)

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(store, "_commit", fail_commit)
    with pytest.raises(SharedAuthorityUnavailable):
        store.record_bootstrap_lifecycle(_event())
    assert store.diagnostics().available is False

    reopened = SharedAuthorityStore(path)
    assert reopened.lifecycle_events() == ()


def test_outbox_projection_is_idempotent_and_external_failure_does_not_ack(tmp_path) -> None:
    store = SharedAuthorityStore(tmp_path / "shared-authority.sqlite3")
    first, _ = store.record_bootstrap_lifecycle(_event())
    second, _ = store.record_bootstrap_lifecycle(_event(offset=1, state="bootstrap_checked"))
    projected: list[str] = []

    assert store.project_outbox(lambda event: projected.append(event.outbox_id)) == 2
    assert projected == [first.outbox_id, second.outbox_id]
    assert store.project_outbox(lambda event: projected.append(event.outbox_id)) == 0

    pending, _ = store.record_bootstrap_lifecycle(_event(offset=2, state="bootstrap_pending"))
    with pytest.raises(SharedAuthorityProjectionError):
        store.project_outbox(lambda _event: (_ for _ in ()).throw(RuntimeError("synthetic")))
    assert store.pending_outbox() == (pending,)


def test_projection_acknowledgement_failure_replays_at_least_once(tmp_path, monkeypatch) -> None:
    path = tmp_path / "shared-authority.sqlite3"
    store = SharedAuthorityStore(path)
    event, _ = store.record_bootstrap_lifecycle(_event())
    calls: list[str] = []

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("synthetic acknowledgement commit failure")

    monkeypatch.setattr(store, "_commit", fail_commit)
    with pytest.raises(SharedAuthorityUnavailable):
        store.project_outbox(lambda outbox: calls.append(outbox.outbox_id))
    assert calls == [event.outbox_id]

    reopened = SharedAuthorityStore(path)
    assert reopened.pending_outbox() == (event,)
    assert reopened.project_outbox(lambda outbox: calls.append(outbox.outbox_id)) == 1
    assert calls == [event.outbox_id, event.outbox_id]


def test_multiple_instances_serialize_lifecycle_versions(tmp_path) -> None:
    path = tmp_path / "shared-authority.sqlite3"
    SharedAuthorityStore(path)

    def append(offset: int):
        return SharedAuthorityStore(path).record_bootstrap_lifecycle(
            _event(offset=offset, state=f"bootstrap_{offset}")
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        events = tuple(pool.map(append, (1, 2)))
    assert {event.lifecycle_version for event in events} == {1, 2}
    assert len(SharedAuthorityStore(path).lifecycle_events()) == 2


def test_bootstrap_store_cannot_reach_existing_execution_or_submission_paths() -> None:
    import app.execution.shared_authority_store as module

    for absent_operation in (
        "register_approval",
        "issue_demo_authorization",
        "reserve",
        "consume_approval",
        "consume_demo_authorization",
        "revoke_approval",
        "revoke_demo_authorization",
    ):
        assert not hasattr(SharedAuthorityStore, absent_operation)

    source = inspect.getsource(module)
    for forbidden in (
        "order_send",
        "MetaTrader5",
        "MT5ExecutionAdapter",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        "ApprovalRegistry",
        "DemoAuthorizationStore",
        ".submit(",
    ):
        assert forbidden not in source
