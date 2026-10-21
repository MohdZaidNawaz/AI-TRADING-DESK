"""Phase 14 local replay tests use only synthetic authority bindings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from app.models.independent_replay import IndependentReplayStoreStatus
from app.models.independent_trust_verification import (
    IndependentReplayBinding,
    IndependentReplayStatus,
)
from app.services.durable_independent_replay import (
    DurableIndependentReplayStore,
    _SCHEMA_V1,
)


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _binding(**changes: object) -> IndependentReplayBinding:
    values: dict[str, object] = {
        "authority_id": "independent-authority",
        "anchor_id": "offline-anchor-1",
        "anchor_version": "v1",
        "anchor_fingerprint": "a" * 64,
        "key_id": "personal-key-1",
        "key_generation": 1,
        "scope_fingerprint": "b" * 64,
        "source_sequence": 1,
        "statement_id": "statement-00000000000000000000001",
        "statement_fingerprint": "c" * 64,
    }
    values.update(changes)
    return IndependentReplayBinding(**values)


def _store(tmp_path: Path) -> DurableIndependentReplayStore:
    return DurableIndependentReplayStore.initialize(tmp_path / "replay.sqlite3", now=NOW)


def test_explicit_initialization_is_required_and_diagnostics_are_non_authorizing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(RuntimeError, match="missing"):
        DurableIndependentReplayStore(path)
    assert not path.exists()

    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    diagnostics = store.diagnostics(checked_at=NOW)
    assert diagnostics.status is IndependentReplayStoreStatus.READY
    assert diagnostics.durable_across_restart is True
    assert diagnostics.atomic_consumption is True
    assert diagnostics.external_rollback_resistance is False
    assert diagnostics.independently_authoritative is False
    assert diagnostics.production_authorization_available is False
    assert diagnostics.submission_allowed is False
    assert diagnostics.execution_authority is False


def test_first_consume_succeeds_and_restart_rejects_exact_replay(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    first = DurableIndependentReplayStore.initialize(path, now=NOW).check_and_record(
        binding=_binding(), checked_at=NOW
    )
    replayed = DurableIndependentReplayStore(path).check_and_record(
        binding=_binding(), checked_at=NOW
    )
    assert first.status is IndependentReplayStatus.ACCEPTED
    assert first.locally_durable is True
    assert first.atomic_consumption is True
    assert first.external_rollback_resistance is False
    assert replayed.status is IndependentReplayStatus.DUPLICATE


def test_sequence_conflict_rollback_and_independent_streams_are_distinct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.check_and_record(binding=_binding(), checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    conflict = store.check_and_record(
        binding=_binding(
            statement_id="statement-00000000000000000000002",
            statement_fingerprint="d" * 64,
        ),
        checked_at=NOW,
    )
    assert conflict.status is IndependentReplayStatus.CONFLICT

    second = store.check_and_record(
        binding=_binding(
            source_sequence=2,
            statement_id="statement-00000000000000000000003",
            statement_fingerprint="e" * 64,
        ),
        checked_at=NOW,
    )
    assert second.status is IndependentReplayStatus.ACCEPTED
    rollback = store.check_and_record(
        binding=_binding(
            statement_id="statement-00000000000000000000004",
            statement_fingerprint="f" * 64,
        ),
        checked_at=NOW,
    )
    assert rollback.status is IndependentReplayStatus.ROLLBACK

    independent = store.check_and_record(
        binding=_binding(
            scope_fingerprint="9" * 64,
            statement_id="statement-00000000000000000000005",
            statement_fingerprint="8" * 64,
        ),
        checked_at=NOW,
    )
    assert independent.status is IndependentReplayStatus.ACCEPTED


def test_concurrent_store_instances_allow_at_most_one_success(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    DurableIndependentReplayStore.initialize(path, now=NOW)

    def consume(_: int) -> IndependentReplayStatus:
        return DurableIndependentReplayStore(path).check_and_record(
            binding=_binding(), checked_at=NOW
        ).status

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = tuple(pool.map(consume, range(8)))
    assert statuses.count(IndependentReplayStatus.ACCEPTED) == 1
    assert all(
        status in {IndependentReplayStatus.ACCEPTED, IndependentReplayStatus.DUPLICATE}
        for status in statuses
    )


def test_commit_failure_never_reports_success_or_persists_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "replay.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)

    def fail_commit(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(DurableIndependentReplayStore, "_commit", staticmethod(fail_commit))
    failed = store.check_and_record(binding=_binding(), checked_at=NOW)
    assert failed.status is IndependentReplayStatus.UNAVAILABLE

    monkeypatch.undo()
    accepted = DurableIndependentReplayStore(path).check_and_record(
        binding=_binding(), checked_at=NOW
    )
    assert accepted.status is IndependentReplayStatus.ACCEPTED


def test_failure_after_commit_is_uncertain_and_cannot_enable_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "replay.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)

    def commit_then_fail(connection: sqlite3.Connection) -> None:
        connection.commit()
        raise sqlite3.OperationalError("synthetic acknowledgement loss")

    monkeypatch.setattr(
        DurableIndependentReplayStore, "_commit", staticmethod(commit_then_fail)
    )
    uncertain = store.check_and_record(binding=_binding(), checked_at=NOW)
    assert uncertain.status is IndependentReplayStatus.UNAVAILABLE

    monkeypatch.undo()
    replayed = DurableIndependentReplayStore(path).check_and_record(
        binding=_binding(), checked_at=NOW
    )
    assert replayed.status is IndependentReplayStatus.DUPLICATE


def test_locked_database_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        result = store.check_and_record(binding=_binding(), checked_at=NOW)
    finally:
        blocker.rollback()
        blocker.close()
    assert result.status is IndependentReplayStatus.UNAVAILABLE


def test_corrupt_unknown_partial_and_weakened_schema_fail_closed(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not-a-sqlite-database")
    with pytest.raises(Exception):
        DurableIndependentReplayStore(corrupt)

    unknown = tmp_path / "unknown.sqlite3"
    DurableIndependentReplayStore.initialize(unknown, now=NOW)
    with sqlite3.connect(unknown) as connection:
        connection.execute("UPDATE replay_schema SET schema_version = 999")
    with pytest.raises(RuntimeError, match="unsupported"):
        DurableIndependentReplayStore(unknown)

    partial = tmp_path / "partial.sqlite3"
    DurableIndependentReplayStore.initialize(partial, now=NOW)
    with sqlite3.connect(partial) as connection:
        connection.execute("UPDATE replay_schema SET migration_state = 'migrating'")
    with pytest.raises(RuntimeError, match="incomplete"):
        DurableIndependentReplayStore(partial)

    weakened = tmp_path / "weakened.sqlite3"
    DurableIndependentReplayStore.initialize(weakened, now=NOW)
    with sqlite3.connect(weakened) as connection:
        connection.execute("DROP INDEX replay_claims_stream_sequence_idx")
    with pytest.raises(RuntimeError, match="structure"):
        DurableIndependentReplayStore.initialize(weakened, now=NOW)
    with sqlite3.connect(weakened) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE name = 'replay_claims_stream_sequence_idx'"
        ).fetchone() == (0,)


def test_supported_migration_preserves_claims_and_adds_history(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    binding = _binding()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO replay_schema VALUES (1, 1, 'ready', ?, ?, ?)",
            ("1" * 32, NOW.isoformat(), NOW.isoformat()),
        )
        stream = "2" * 64
        connection.execute(
            "INSERT INTO replay_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding.binding_fingerprint,
                stream,
                binding.authority_id,
                binding.anchor_id,
                binding.anchor_version,
                binding.anchor_fingerprint,
                binding.key_id,
                binding.key_generation,
                binding.scope_fingerprint,
                binding.source_sequence,
                binding.statement_id,
                binding.statement_fingerprint,
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO replay_heads VALUES (?, ?, ?, ?)",
            (stream, 1, binding.binding_fingerprint, NOW.isoformat()),
        )

    migrated = DurableIndependentReplayStore.initialize(path, now=NOW)
    assert migrated.diagnostics(checked_at=NOW).database_schema_version == 2
    assert migrated.check_and_record(binding=binding, checked_at=NOW).status is IndependentReplayStatus.DUPLICATE
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM replay_events").fetchone() == (1,)


def test_failed_migration_preserves_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO replay_schema VALUES (1, 1, 'ready', ?, ?, ?)",
            ("3" * 32, NOW.isoformat(), NOW.isoformat()),
        )

    def fail_commit(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("synthetic migration commit failure")

    monkeypatch.setattr(DurableIndependentReplayStore, "_commit", staticmethod(fail_commit))
    with pytest.raises(sqlite3.OperationalError):
        DurableIndependentReplayStore.initialize(path, now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT schema_version, migration_state FROM replay_schema"
        ).fetchone() == (1, "ready")
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'replay_events'"
        ).fetchone() == (0,)


def test_models_reject_fabricated_external_rollback_assurance(tmp_path: Path) -> None:
    diagnostics = _store(tmp_path).diagnostics(checked_at=NOW)
    payload = diagnostics.model_dump(exclude={"fingerprint"})
    payload["external_rollback_resistance"] = True
    with pytest.raises(Exception):
        type(diagnostics).model_validate(payload)
