"""Fail-closed local SQLite replay consumption for signed authority evidence.

This store provides atomic consumption across processes that share one SQLite
database and preserves claims across ordinary restarts.  It deliberately does
not claim resistance to a privileged local rollback of the entire database.
The database must be explicitly initialized; verification never creates or
repairs missing/corrupt state.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Final
from uuid import uuid4

from app.models.independent_replay import (
    AtomicReplayConsumptionResult,
    IndependentReplayStoreDiagnostics,
    IndependentReplayStoreStatus,
)
from app.models.independent_trust_verification import (
    IndependentReplayBinding,
    IndependentReplayResult,
    IndependentReplayStatus,
)


_SCHEMA_VERSION: Final = 2
_SUPPORTED_LEGACY_VERSION: Final = 1
_TABLES: Final = frozenset(
    {"replay_schema", "replay_claims", "replay_heads", "replay_events"}
)

_SCHEMA_V1: Final = (
    """
    CREATE TABLE replay_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
        migration_state TEXT NOT NULL CHECK (migration_state IN ('ready', 'migrating')),
        store_id TEXT NOT NULL CHECK (length(store_id) = 32),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE replay_claims (
        binding_fingerprint TEXT PRIMARY KEY CHECK (length(binding_fingerprint) = 64),
        stream_fingerprint TEXT NOT NULL CHECK (length(stream_fingerprint) = 64),
        authority_id TEXT NOT NULL,
        anchor_id TEXT NOT NULL,
        anchor_version TEXT NOT NULL,
        anchor_fingerprint TEXT NOT NULL CHECK (length(anchor_fingerprint) = 64),
        key_id TEXT NOT NULL,
        key_generation INTEGER NOT NULL CHECK (key_generation >= 1),
        scope_fingerprint TEXT NOT NULL CHECK (length(scope_fingerprint) = 64),
        source_sequence INTEGER NOT NULL CHECK (source_sequence >= 1),
        statement_id TEXT NOT NULL,
        statement_fingerprint TEXT NOT NULL CHECK (length(statement_fingerprint) = 64),
        consumed_at TEXT NOT NULL,
        UNIQUE (stream_fingerprint, source_sequence)
    )
    """,
    """
    CREATE TABLE replay_heads (
        stream_fingerprint TEXT PRIMARY KEY CHECK (length(stream_fingerprint) = 64),
        last_sequence INTEGER NOT NULL CHECK (last_sequence >= 1),
        binding_fingerprint TEXT NOT NULL UNIQUE,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (binding_fingerprint) REFERENCES replay_claims(binding_fingerprint)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX replay_claims_stream_sequence_idx
        ON replay_claims(stream_fingerprint, source_sequence)
    """,
)

_SCHEMA_V2_EVENT: Final = """
    CREATE TABLE replay_events (
        event_id TEXT PRIMARY KEY CHECK (length(event_id) = 64),
        event_type TEXT NOT NULL CHECK (event_type = 'replay_consumed'),
        binding_fingerprint TEXT NOT NULL UNIQUE,
        occurred_at TEXT NOT NULL,
        FOREIGN KEY (binding_fingerprint) REFERENCES replay_claims(binding_fingerprint)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
"""

_EXPECTED_SQL: Final = {
    "replay_schema": _SCHEMA_V1[0],
    "replay_claims": _SCHEMA_V1[1],
    "replay_heads": _SCHEMA_V1[2],
    "replay_events": _SCHEMA_V2_EVENT,
    "replay_claims_stream_sequence_idx": _SCHEMA_V1[3],
}


class _ReplaySemanticIntegrityError(RuntimeError):
    """Internal non-secret signal for coherent but contradictory replay state."""


class DurableIndependentReplayStore:
    """Explicitly initialized, local-only replay store.

    The successful transaction commit is the linearization point.  A result is
    never reported as accepted before the claim, stream head, and event have all
    committed in the same database transaction.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        if not self._path.is_file():
            raise RuntimeError("independent replay database is missing")
        self._validate_database()

    @classmethod
    def initialize(cls, database_path: str | Path, *, now: datetime) -> "DurableIndependentReplayStore":
        """Explicitly create, validate, or safely upgrade the local store."""

        path = Path(database_path)
        timestamp = _utc(now).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
            try:
                _configure(connection)
                connection.execute("BEGIN EXCLUSIVE")
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(_SCHEMA_V2_EVENT)
                connection.execute(
                    "INSERT INTO replay_schema "
                    "(singleton, schema_version, migration_state, store_id, created_at, updated_at) "
                    "VALUES (1, ?, 'ready', ?, ?, ?)",
                    (_SCHEMA_VERSION, uuid4().hex, timestamp, timestamp),
                )
                _validated_ready_row(connection)
                cls._commit(connection)
            except Exception:
                _safe_rollback(connection)
                raise RuntimeError("independent replay database initialization failed") from None
            finally:
                connection.close()
        else:
            cls._upgrade_if_supported(path=path, now=timestamp)
        return cls(path)

    @classmethod
    def _upgrade_if_supported(cls, *, path: Path, now: str) -> None:
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            _configure(connection)
            connection.execute("BEGIN EXCLUSIVE")
            _validate_physical_integrity(connection)
            row = _read_schema_row(connection)
            if row is None:
                raise RuntimeError("independent replay schema metadata is missing")
            version, state = int(row[0]), str(row[1])
            if state != "ready":
                raise RuntimeError("independent replay migration is incomplete")
            if version == _SCHEMA_VERSION:
                _verify_schema(connection, version=_SCHEMA_VERSION)
                _validate_semantic_integrity(connection, version=_SCHEMA_VERSION)
                connection.rollback()
                return
            if version != _SUPPORTED_LEGACY_VERSION:
                raise RuntimeError("independent replay schema version is unsupported")
            _verify_schema(connection, version=_SUPPORTED_LEGACY_VERSION)
            _validate_semantic_integrity(connection, version=_SUPPORTED_LEGACY_VERSION)
            connection.execute(
                "UPDATE replay_schema SET migration_state = 'migrating', updated_at = ? "
                "WHERE singleton = 1 AND schema_version = ? AND migration_state = 'ready'",
                (now, _SUPPORTED_LEGACY_VERSION),
            )
            if connection.total_changes != 1:
                raise RuntimeError("independent replay migration precondition failed")
            connection.execute(_SCHEMA_V2_EVENT)
            claims = connection.execute(
                "SELECT binding_fingerprint, consumed_at FROM replay_claims "
                "ORDER BY binding_fingerprint"
            ).fetchall()
            for binding_fingerprint, consumed_at in claims:
                connection.execute(
                    "INSERT INTO replay_events "
                    "(event_id, event_type, binding_fingerprint, occurred_at) "
                    "VALUES (?, 'replay_consumed', ?, ?)",
                    (_event_id(str(binding_fingerprint)), binding_fingerprint, consumed_at),
                )
            connection.execute(
                "UPDATE replay_schema SET schema_version = ?, migration_state = 'ready', "
                "updated_at = ? WHERE singleton = 1 AND migration_state = 'migrating'",
                (_SCHEMA_VERSION, now),
            )
            _validated_ready_row(connection)
            cls._commit(connection)
        except Exception:
            _safe_rollback(connection)
            raise
        finally:
            connection.close()

    def diagnostics(self, *, checked_at: datetime) -> IndependentReplayStoreDiagnostics:
        now = _utc(checked_at)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                row = _validated_ready_row(connection)
                connection.rollback()
            return IndependentReplayStoreDiagnostics(
                status=IndependentReplayStoreStatus.READY,
                checked_at=now,
                database_schema_version=int(row[0]),
                store_id=str(row[2]),
                durable_across_restart=True,
                atomic_consumption=True,
            )
        except Exception as error:
            status, code = _diagnostic_failure(error)
            return IndependentReplayStoreDiagnostics(
                status=status,
                checked_at=now,
                reason_codes=(code,),
            )

    def check_and_record(
        self, *, binding: IndependentReplayBinding, checked_at: datetime
    ) -> IndependentReplayResult:
        now = _utc(checked_at)
        fingerprint = binding.binding_fingerprint
        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            _validated_ready_row(connection)
            stream_fingerprint = _stream_fingerprint(binding)
            head = connection.execute(
                "SELECT last_sequence, binding_fingerprint FROM replay_heads "
                "WHERE stream_fingerprint = ?",
                (stream_fingerprint,),
            ).fetchone()
            if head is not None and binding.source_sequence < int(head[0]):
                duplicate = connection.execute(
                    "SELECT 1 FROM replay_claims WHERE binding_fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                connection.rollback()
                if duplicate is not None:
                    return _result(
                        IndependentReplayStatus.DUPLICATE,
                        binding,
                        now,
                        "AUTHORITY_REPLAY_DUPLICATE",
                    )
                return _result(
                    IndependentReplayStatus.ROLLBACK,
                    binding,
                    now,
                    "AUTHORITY_REPLAY_SEQUENCE_ROLLBACK",
                )
            if head is not None and binding.source_sequence == int(head[0]):
                if str(head[1]) != fingerprint:
                    connection.rollback()
                    return _result(
                        IndependentReplayStatus.CONFLICT,
                        binding,
                        now,
                        "AUTHORITY_REPLAY_SEQUENCE_CONFLICT",
                    )

            consumed_at = now.isoformat()
            _insert_claim(connection, binding, stream_fingerprint, consumed_at)
            connection.execute(
                "INSERT INTO replay_heads "
                "(stream_fingerprint, last_sequence, binding_fingerprint, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(stream_fingerprint) DO UPDATE SET "
                "last_sequence = excluded.last_sequence, "
                "binding_fingerprint = excluded.binding_fingerprint, "
                "updated_at = excluded.updated_at "
                "WHERE replay_heads.last_sequence < excluded.last_sequence",
                (stream_fingerprint, binding.source_sequence, fingerprint, consumed_at),
            )
            _insert_event(connection, fingerprint, consumed_at)
            _validate_semantic_integrity(connection, version=_SCHEMA_VERSION)
            accepted = IndependentReplayResult(
                status=IndependentReplayStatus.ACCEPTED,
                binding_fingerprint=fingerprint,
                checked_at=now,
                locally_durable=True,
                atomic_consumption=True,
            )
            self._commit(connection)
            committed = True
            return accepted
        except sqlite3.IntegrityError as error:
            _safe_rollback(connection)
            if _is_replay_claim_uniqueness_error(error) and self._persisted_claim_matches(
                binding
            ):
                return _result(
                    IndependentReplayStatus.DUPLICATE,
                    binding,
                    now,
                    "AUTHORITY_REPLAY_DUPLICATE",
                )
            return _result(
                IndependentReplayStatus.UNAVAILABLE,
                binding,
                now,
                "DURABLE_REPLAY_STORAGE_INTEGRITY_FAILURE",
            )
        except _ReplaySemanticIntegrityError:
            _safe_rollback(connection)
            return _result(
                IndependentReplayStatus.UNAVAILABLE,
                binding,
                now,
                "DURABLE_REPLAY_SEMANTIC_INTEGRITY_FAILURE",
            )
        except Exception:
            if not committed:
                _safe_rollback(connection)
            return _result(
                IndependentReplayStatus.UNAVAILABLE,
                binding,
                now,
                "DURABLE_REPLAY_STORAGE_UNAVAILABLE",
            )
        finally:
            if connection is not None:
                connection.close()

    def check_and_record_atomic(
        self,
        *,
        bindings: tuple[IndependentReplayBinding, ...],
        checked_at: datetime,
    ) -> AtomicReplayConsumptionResult:
        """Consume related replay claims in one transaction.

        Phase 18 uses one binding for challenge uniqueness and another for the
        provider sequence stream.  No claim, head, or history event survives
        unless every binding is valid and the transaction commits.
        """

        now = _utc(checked_at)
        fingerprints = tuple(binding.binding_fingerprint for binding in bindings)
        streams = tuple(_stream_fingerprint(binding) for binding in bindings)
        if (
            not bindings
            or len(set(fingerprints)) != len(fingerprints)
            or len(set(streams)) != len(streams)
        ):
            return _atomic_result(
                "unavailable", fingerprints or ("0" * 64,), now,
                "DURABLE_REPLAY_ATOMIC_BINDINGS_INVALID",
            )
        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            _validated_ready_row(connection)
            for binding in bindings:
                status, reason = _binding_precondition(connection, binding)
                if status is not None:
                    connection.rollback()
                    return _atomic_result(status, fingerprints, now, reason)

            consumed_at = now.isoformat()
            for binding in bindings:
                stream_fingerprint = _stream_fingerprint(binding)
                _insert_claim(connection, binding, stream_fingerprint, consumed_at)
                connection.execute(
                    "INSERT INTO replay_heads "
                    "(stream_fingerprint, last_sequence, binding_fingerprint, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(stream_fingerprint) DO UPDATE SET "
                    "last_sequence = excluded.last_sequence, "
                    "binding_fingerprint = excluded.binding_fingerprint, "
                    "updated_at = excluded.updated_at "
                    "WHERE replay_heads.last_sequence < excluded.last_sequence",
                    (
                        stream_fingerprint,
                        binding.source_sequence,
                        binding.binding_fingerprint,
                        consumed_at,
                    ),
                )
                _insert_event(connection, binding.binding_fingerprint, consumed_at)
            _validate_semantic_integrity(connection, version=_SCHEMA_VERSION)
            self._commit(connection)
            committed = True
            return AtomicReplayConsumptionResult(
                status="accepted",
                binding_fingerprints=fingerprints,
                checked_at=now,
                locally_durable=True,
                atomic_consumption=True,
            )
        except sqlite3.IntegrityError:
            _safe_rollback(connection)
            return _atomic_result(
                "unavailable", fingerprints, now,
                "DURABLE_REPLAY_STORAGE_INTEGRITY_FAILURE",
            )
        except _ReplaySemanticIntegrityError:
            _safe_rollback(connection)
            return _atomic_result(
                "unavailable", fingerprints, now,
                "DURABLE_REPLAY_SEMANTIC_INTEGRITY_FAILURE",
            )
        except Exception:
            if not committed:
                _safe_rollback(connection)
            return _atomic_result(
                "unavailable", fingerprints, now,
                "DURABLE_REPLAY_STORAGE_UNAVAILABLE",
            )
        finally:
            if connection is not None:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        if not self._path.is_file():
            raise RuntimeError("independent replay database is missing")
        connection = sqlite3.connect(self._path, timeout=1.0, isolation_level=None)
        _configure(connection)
        return connection

    def _validate_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            _validated_ready_row(connection)
            connection.rollback()

    def _persisted_claim_matches(self, binding: IndependentReplayBinding) -> bool:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                _validated_ready_row(connection)
                row = connection.execute(
                    "SELECT stream_fingerprint, authority_id, anchor_id, anchor_version, "
                    "anchor_fingerprint, key_id, key_generation, scope_fingerprint, "
                    "source_sequence, statement_id, statement_fingerprint "
                    "FROM replay_claims WHERE binding_fingerprint = ?",
                    (binding.binding_fingerprint,),
                ).fetchone()
                connection.rollback()
            return row == _claim_identity(binding)
        except Exception:
            return False

    @staticmethod
    def _commit(connection: sqlite3.Connection) -> None:
        connection.commit()


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 1000")
    connection.execute("PRAGMA synchronous = FULL")
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("SQLite foreign-key enforcement is unavailable")


def _validated_ready_row(connection: sqlite3.Connection) -> tuple[object, ...]:
    _validate_physical_integrity(connection)
    row = _read_schema_row(connection)
    if row is None:
        raise RuntimeError("independent replay schema metadata is missing")
    version, state = int(row[0]), str(row[1])
    if version != _SCHEMA_VERSION:
        raise RuntimeError("independent replay schema version is unsupported")
    if state != "ready":
        raise RuntimeError("independent replay migration is incomplete")
    _verify_schema(connection, version=version)
    _validate_semantic_integrity(connection, version=version)
    return row


def _validate_physical_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA quick_check").fetchone()
    if integrity != ("ok",):
        raise RuntimeError("independent replay database integrity check failed")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError("independent replay database foreign keys are invalid")


def _read_schema_row(connection: sqlite3.Connection) -> tuple[object, ...] | None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if "replay_schema" not in tables:
        return None
    return connection.execute(
        "SELECT schema_version, migration_state, store_id, created_at, updated_at "
        "FROM replay_schema WHERE singleton = 1"
    ).fetchone()


def _verify_schema(connection: sqlite3.Connection, *, version: int) -> None:
    expected_tables = _TABLES if version == _SCHEMA_VERSION else _TABLES - {"replay_events"}
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if tables != expected_tables:
        raise RuntimeError("independent replay schema tables are incompatible")
    names = set(expected_tables) | {"replay_claims_stream_sequence_idx"}
    for name in names:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ? AND type IN ('table', 'index')",
            (name,),
        ).fetchone()
        expected = _EXPECTED_SQL[name]
        if row is None or _normalize_sql(str(row[0])) != _normalize_sql(expected):
            raise RuntimeError("independent replay schema structure is incompatible")


def _validate_semantic_integrity(connection: sqlite3.Connection, *, version: int) -> None:
    """Validate cross-table replay invariants without repairing persisted state."""

    try:
        claim_rows = connection.execute(
            "SELECT binding_fingerprint, stream_fingerprint, authority_id, anchor_id, "
            "anchor_version, anchor_fingerprint, key_id, key_generation, "
            "scope_fingerprint, source_sequence, statement_id, statement_fingerprint, "
            "consumed_at FROM replay_claims ORDER BY binding_fingerprint"
        ).fetchall()
        claims: dict[str, tuple[str, int, datetime]] = {}
        streams: dict[str, list[tuple[int, str, datetime]]] = {}
        for row in claim_rows:
            binding = IndependentReplayBinding(
                authority_id=row[2],
                anchor_id=row[3],
                anchor_version=row[4],
                anchor_fingerprint=row[5],
                key_id=row[6],
                key_generation=row[7],
                scope_fingerprint=row[8],
                source_sequence=row[9],
                statement_id=row[10],
                statement_fingerprint=row[11],
            )
            binding_fingerprint = str(row[0])
            stream_fingerprint = str(row[1])
            consumed_at = _stored_time(row[12])
            if binding.binding_fingerprint != binding_fingerprint:
                raise _ReplaySemanticIntegrityError(
                    "independent replay claim fingerprint is inconsistent"
                )
            if _stream_fingerprint(binding) != stream_fingerprint:
                raise _ReplaySemanticIntegrityError(
                    "independent replay claim stream is inconsistent"
                )
            claims[binding_fingerprint] = (
                stream_fingerprint,
                binding.source_sequence,
                consumed_at,
            )
            streams.setdefault(stream_fingerprint, []).append(
                (binding.source_sequence, binding_fingerprint, consumed_at)
            )

        head_rows = connection.execute(
            "SELECT stream_fingerprint, last_sequence, binding_fingerprint, updated_at "
            "FROM replay_heads ORDER BY stream_fingerprint"
        ).fetchall()
        heads = {str(row[0]): row for row in head_rows}
        if set(heads) != set(streams):
            raise _ReplaySemanticIntegrityError(
                "independent replay streams and heads are inconsistent"
            )
        for stream_fingerprint, stream_claims in streams.items():
            maximum = max(stream_claims, key=lambda item: item[0])
            head = heads[stream_fingerprint]
            head_sequence = int(head[1])
            head_binding = str(head[2])
            head_time = _stored_time(head[3])
            if head_sequence != maximum[0] or head_binding != maximum[1]:
                raise _ReplaySemanticIntegrityError(
                    "independent replay stream head is not the retained maximum"
                )
            claim = claims.get(head_binding)
            if claim is None or claim[0] != stream_fingerprint or claim[1] != head_sequence:
                raise _ReplaySemanticIntegrityError(
                    "independent replay stream head references the wrong claim"
                )
            if head_time != maximum[2]:
                raise _ReplaySemanticIntegrityError(
                    "independent replay stream head timestamp is inconsistent"
                )

        if version == _SCHEMA_VERSION:
            event_rows = connection.execute(
                "SELECT event_id, event_type, binding_fingerprint, occurred_at "
                "FROM replay_events ORDER BY binding_fingerprint"
            ).fetchall()
            events = {str(row[2]): row for row in event_rows}
            if len(events) != len(event_rows) or set(events) != set(claims):
                raise _ReplaySemanticIntegrityError(
                    "independent replay claims and history are inconsistent"
                )
            for binding_fingerprint, claim in claims.items():
                event = events[binding_fingerprint]
                if (
                    str(event[0]) != _event_id(binding_fingerprint)
                    or str(event[1]) != "replay_consumed"
                    or _stored_time(event[3]) != claim[2]
                ):
                    raise _ReplaySemanticIntegrityError(
                        "independent replay history does not match its claim"
                    )
    except _ReplaySemanticIntegrityError:
        raise
    except Exception:
        raise _ReplaySemanticIntegrityError(
            "independent replay semantic integrity validation failed"
        ) from None


def _normalize_sql(value: str) -> str:
    return " ".join(value.replace("\"", "").replace("`", "").split()).strip().lower()


def _stream_fingerprint(binding: IndependentReplayBinding) -> str:
    value = {
        "authority_id": binding.authority_id,
        "anchor_id": binding.anchor_id,
        "anchor_version": binding.anchor_version,
        "anchor_fingerprint": binding.anchor_fingerprint,
        "key_id": binding.key_id,
        "key_generation": binding.key_generation,
        "scope_fingerprint": binding.scope_fingerprint,
    }
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _binding_precondition(
    connection: sqlite3.Connection, binding: IndependentReplayBinding
) -> tuple[str | None, str]:
    stream_fingerprint = _stream_fingerprint(binding)
    head = connection.execute(
        "SELECT last_sequence, binding_fingerprint FROM replay_heads "
        "WHERE stream_fingerprint = ?",
        (stream_fingerprint,),
    ).fetchone()
    if head is None:
        return None, ""
    if binding.source_sequence < int(head[0]):
        duplicate = connection.execute(
            "SELECT 1 FROM replay_claims WHERE binding_fingerprint = ?",
            (binding.binding_fingerprint,),
        ).fetchone()
        return (
            ("duplicate", "AUTHORITY_REPLAY_DUPLICATE")
            if duplicate is not None
            else ("rollback", "AUTHORITY_REPLAY_SEQUENCE_ROLLBACK")
        )
    if binding.source_sequence == int(head[0]):
        return (
            ("duplicate", "AUTHORITY_REPLAY_DUPLICATE")
            if str(head[1]) == binding.binding_fingerprint
            else ("conflict", "AUTHORITY_REPLAY_SEQUENCE_CONFLICT")
        )
    return None, ""


def _claim_identity(binding: IndependentReplayBinding) -> tuple[object, ...]:
    return (
        _stream_fingerprint(binding),
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
    )


def _insert_claim(
    connection: sqlite3.Connection,
    binding: IndependentReplayBinding,
    stream_fingerprint: str,
    consumed_at: str,
) -> None:
    connection.execute(
        "INSERT INTO replay_claims "
        "(binding_fingerprint, stream_fingerprint, authority_id, anchor_id, "
        "anchor_version, anchor_fingerprint, key_id, key_generation, "
        "scope_fingerprint, source_sequence, statement_id, "
        "statement_fingerprint, consumed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            binding.binding_fingerprint,
            stream_fingerprint,
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
            consumed_at,
        ),
    )


def _insert_event(
    connection: sqlite3.Connection, binding_fingerprint: str, occurred_at: str
) -> None:
    connection.execute(
        "INSERT INTO replay_events "
        "(event_id, event_type, binding_fingerprint, occurred_at) "
        "VALUES (?, 'replay_consumed', ?, ?)",
        (_event_id(binding_fingerprint), binding_fingerprint, occurred_at),
    )


def _is_replay_claim_uniqueness_error(error: sqlite3.IntegrityError) -> bool:
    return (
        getattr(error, "sqlite_errorname", "")
        in {"SQLITE_CONSTRAINT_PRIMARYKEY", "SQLITE_CONSTRAINT_UNIQUE"}
        and str(error)
        in {
            "UNIQUE constraint failed: replay_claims.binding_fingerprint",
            "UNIQUE constraint failed: replay_claims.stream_fingerprint, "
            "replay_claims.source_sequence",
        }
    )


def _stored_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise _ReplaySemanticIntegrityError(
            "independent replay timestamp is malformed"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _ReplaySemanticIntegrityError(
            "independent replay timestamp lacks timezone awareness"
        )
    return parsed.astimezone(UTC)


def _event_id(binding_fingerprint: str) -> str:
    return sha256(f"independent-replay-consumed/v1:{binding_fingerprint}".encode()).hexdigest()


def _result(
    status: IndependentReplayStatus,
    binding: IndependentReplayBinding,
    checked_at: datetime,
    reason: str,
) -> IndependentReplayResult:
    return IndependentReplayResult(
        status=status,
        binding_fingerprint=binding.binding_fingerprint,
        checked_at=checked_at,
        reason_codes=(reason,),
        locally_durable=(status is not IndependentReplayStatus.UNAVAILABLE),
        atomic_consumption=(status is not IndependentReplayStatus.UNAVAILABLE),
    )


def _atomic_result(
    status: str,
    fingerprints: tuple[str, ...],
    checked_at: datetime,
    reason: str,
) -> AtomicReplayConsumptionResult:
    return AtomicReplayConsumptionResult(
        status=status,
        binding_fingerprints=fingerprints,
        checked_at=checked_at,
        reason_codes=(reason,),
    )


def _diagnostic_failure(error: Exception) -> tuple[IndependentReplayStoreStatus, str]:
    message = str(error).lower()
    if "missing" in message:
        return IndependentReplayStoreStatus.MISSING, "DURABLE_REPLAY_DATABASE_MISSING"
    if "unsupported" in message:
        return IndependentReplayStoreStatus.UNSUPPORTED, "DURABLE_REPLAY_SCHEMA_UNSUPPORTED"
    if "migration" in message:
        return (
            IndependentReplayStoreStatus.MIGRATION_INCOMPLETE,
            "DURABLE_REPLAY_MIGRATION_INCOMPLETE",
        )
    if "integrity" in message or "corrupt" in message or "malformed" in message:
        return IndependentReplayStoreStatus.CORRUPT, "DURABLE_REPLAY_DATABASE_CORRUPT"
    return IndependentReplayStoreStatus.UNAVAILABLE, "DURABLE_REPLAY_STORAGE_UNAVAILABLE"


def _safe_rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except Exception:
        pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("replay store timestamps require timezone awareness")
    return value.astimezone(UTC)
