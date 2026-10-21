"""Phase 14 local replay tests use only synthetic authority bindings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

import app.services.durable_independent_replay as replay_module
from app.models.independent_replay import IndependentReplayStoreStatus
from app.models.independent_trust_verification import (
    IndependentReplayBinding,
    IndependentReplayStatus,
)
from app.services.durable_independent_replay import (
    DurableIndependentReplayStore,
    _SCHEMA_V1,
    _stream_fingerprint,
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


def _two_claim_store(tmp_path: Path) -> tuple[Path, IndependentReplayBinding, IndependentReplayBinding]:
    path = tmp_path / "replay.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    first = _binding()
    second = _binding(
        source_sequence=2,
        statement_id="statement-00000000000000000000002",
        statement_fingerprint="d" * 64,
    )
    assert store.check_and_record(binding=first, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    assert store.check_and_record(binding=second, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    return path, first, second


def _create_legacy_store(
    path: Path, binding: IndependentReplayBinding, *, include_head: bool = True
) -> None:
    stream = _stream_fingerprint(binding)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO replay_schema VALUES (1, 1, 'ready', ?, ?, ?)",
            ("1" * 32, NOW.isoformat(), NOW.isoformat()),
        )
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
        if include_head:
            connection.execute(
                "INSERT INTO replay_heads VALUES (?, ?, ?, ?)",
                (stream, binding.source_sequence, binding.binding_fingerprint, NOW.isoformat()),
            )


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
    assert replayed.reason_codes == ("AUTHORITY_REPLAY_DUPLICATE",)
    assert replayed.locally_durable is True
    assert replayed.atomic_consumption is True


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
    _create_legacy_store(path, binding)

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


def test_missing_stream_head_is_rejected_without_repair(tmp_path: Path) -> None:
    path, first, _second = _two_claim_store(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM replay_heads")

    with pytest.raises(RuntimeError, match="streams and heads"):
        DurableIndependentReplayStore.initialize(path, now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM replay_heads").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM replay_claims").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM replay_claims WHERE binding_fingerprint = ?",
            (first.binding_fingerprint,),
        ).fetchone() == (1,)


def test_stale_stream_head_below_maximum_is_rejected(tmp_path: Path) -> None:
    path, first, _second = _two_claim_store(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE replay_heads SET last_sequence = 1, binding_fingerprint = ?, updated_at = ?",
            (first.binding_fingerprint, NOW.isoformat()),
        )

    with pytest.raises(RuntimeError, match="retained maximum"):
        DurableIndependentReplayStore(path)


def test_claim_and_head_cannot_move_to_a_fabricated_stream(tmp_path: Path) -> None:
    path = tmp_path / "fabricated-stream.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    binding = _binding()
    assert store.check_and_record(binding=binding, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE replay_claims SET stream_fingerprint = ?",
            ("f" * 64,),
        )
        connection.execute(
            "UPDATE replay_heads SET stream_fingerprint = ?",
            ("f" * 64,),
        )

    with pytest.raises(RuntimeError, match="claim stream"):
        DurableIndependentReplayStore(path)


def test_head_referencing_missing_or_wrong_stream_claim_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing-claim.sqlite3"
    store = DurableIndependentReplayStore.initialize(missing, now=NOW)
    first = _binding()
    assert store.check_and_record(binding=first, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(missing) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM replay_claims WHERE binding_fingerprint = ?",
            (first.binding_fingerprint,),
        )
    with pytest.raises(RuntimeError, match="foreign keys"):
        DurableIndependentReplayStore(missing)

    wrong = tmp_path / "wrong-stream.sqlite3"
    store = DurableIndependentReplayStore.initialize(wrong, now=NOW)
    second_stream = _binding(
        scope_fingerprint="9" * 64,
        statement_id="statement-00000000000000000000009",
        statement_fingerprint="8" * 64,
    )
    assert store.check_and_record(binding=first, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    assert store.check_and_record(binding=second_stream, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(wrong) as connection:
        connection.execute(
            "DELETE FROM replay_heads WHERE stream_fingerprint = ?",
            (_stream_fingerprint(second_stream),),
        )
        connection.execute(
            "UPDATE replay_heads SET binding_fingerprint = ?",
            (second_stream.binding_fingerprint,),
        )
    with pytest.raises(RuntimeError, match="streams and heads|wrong claim"):
        DurableIndependentReplayStore(wrong)


def test_missing_or_inconsistent_history_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing-event.sqlite3"
    store = DurableIndependentReplayStore.initialize(missing, now=NOW)
    binding = _binding()
    assert store.check_and_record(binding=binding, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(missing) as connection:
        connection.execute("DELETE FROM replay_events")
    with pytest.raises(RuntimeError, match="claims and history"):
        DurableIndependentReplayStore(missing)

    inconsistent = tmp_path / "inconsistent-event.sqlite3"
    store = DurableIndependentReplayStore.initialize(inconsistent, now=NOW)
    assert store.check_and_record(binding=binding, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(inconsistent) as connection:
        connection.execute("UPDATE replay_events SET occurred_at = ?", ((NOW.replace(hour=13)).isoformat(),))
    with pytest.raises(RuntimeError, match="history does not match"):
        DurableIndependentReplayStore(inconsistent)

    wrong_identity = tmp_path / "wrong-event-identity.sqlite3"
    store = DurableIndependentReplayStore.initialize(wrong_identity, now=NOW)
    assert store.check_and_record(binding=binding, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(wrong_identity) as connection:
        connection.execute("UPDATE replay_events SET event_id = ?", ("9" * 64,))
    with pytest.raises(RuntimeError, match="history does not match"):
        DurableIndependentReplayStore(wrong_identity)


def test_orphan_history_event_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "orphan-event.sqlite3"
    DurableIndependentReplayStore.initialize(path, now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO replay_events VALUES (?, 'replay_consumed', ?, ?)",
            ("7" * 64, "8" * 64, NOW.isoformat()),
        )
    with pytest.raises(RuntimeError, match="foreign keys"):
        DurableIndependentReplayStore(path)


def test_semantically_inconsistent_legacy_state_is_not_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy-inconsistent.sqlite3"
    _create_legacy_store(path, _binding(), include_head=False)

    with pytest.raises(RuntimeError, match="streams and heads"):
        DurableIndependentReplayStore.initialize(path, now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT schema_version, migration_state FROM replay_schema"
        ).fetchone() == (1, "ready")
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'replay_events'"
        ).fetchone() == (0,)


def test_semantic_corruption_fails_closed_during_consumption_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-during-consume.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    first = _binding()
    assert store.check_and_record(binding=first, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM replay_heads")

    second = _binding(
        source_sequence=2,
        statement_id="statement-00000000000000000000002",
        statement_fingerprint="d" * 64,
    )
    result = store.check_and_record(binding=second, checked_at=NOW)
    assert result.status is IndependentReplayStatus.UNAVAILABLE
    assert result.reason_codes == ("DURABLE_REPLAY_SEMANTIC_INTEGRITY_FAILURE",)
    assert result.locally_durable is False
    assert result.atomic_consumption is False
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM replay_heads").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM replay_claims").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM replay_events").fetchone() == (1,)


def test_unrelated_integrity_failure_is_unavailable_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "integrity-failure.sqlite3"
    store = DurableIndependentReplayStore.initialize(path, now=NOW)
    first = _binding()
    assert store.check_and_record(binding=first, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED

    scratch = sqlite3.connect(":memory:")
    scratch.execute("CREATE TABLE constrained (value INTEGER CHECK (value > 0))")
    try:
        with pytest.raises(sqlite3.IntegrityError) as captured:
            scratch.execute("INSERT INTO constrained VALUES (0)")
    finally:
        scratch.close()

    def fail_event(*_args: object, **_kwargs: object) -> None:
        raise captured.value

    monkeypatch.setattr(replay_module, "_insert_event", fail_event)
    second = _binding(
        source_sequence=2,
        statement_id="statement-00000000000000000000002",
        statement_fingerprint="d" * 64,
    )
    failed = store.check_and_record(binding=second, checked_at=NOW)
    assert failed.status is IndependentReplayStatus.UNAVAILABLE
    assert failed.reason_codes == ("DURABLE_REPLAY_STORAGE_INTEGRITY_FAILURE",)
    assert failed.locally_durable is False
    assert failed.atomic_consumption is False

    monkeypatch.undo()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT max(source_sequence) FROM replay_claims").fetchone() == (1,)
        assert connection.execute("SELECT last_sequence FROM replay_heads").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM replay_events").fetchone() == (1,)
    assert store.check_and_record(binding=second, checked_at=NOW).status is IndependentReplayStatus.ACCEPTED
