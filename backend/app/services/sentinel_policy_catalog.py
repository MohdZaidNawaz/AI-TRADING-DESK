"""Durable local catalog for policy history; never an approval authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from app.models.sentinel_policy_authority import (
    SentinelPolicyAuthorityLevel,
    SentinelPolicyLifecycle,
    SentinelPortfolioRiskPolicyRecord,
)


_SCHEMA_VERSION = "1"
_TABLE_SQL = (
    "CREATE TABLE sentinel_policy_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE sentinel_policy_records (
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active','revoked','superseded')),
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    content_fingerprint TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, policy_version)
    )""",
    """CREATE TABLE sentinel_policy_events (
    event_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    UNIQUE (policy_id, policy_version, lifecycle_version),
    FOREIGN KEY (policy_id, policy_version)
      REFERENCES sentinel_policy_records(policy_id, policy_version)
      ON DELETE RESTRICT
    )""",
)
_INDEX_SQL = (
    """CREATE UNIQUE INDEX sentinel_policy_one_active_version
    ON sentinel_policy_records (policy_id) WHERE lifecycle = 'active'""",
)


class SentinelPolicyCatalogError(RuntimeError):
    pass


class LocalSentinelPolicyCatalog:
    """Restart-safe lifecycle catalog with no independent approval semantics.

    Only ``LOCAL_GOVERNANCE_ONLY`` records can be stored.  Consequently, a row
    in this database can support audit/replay but can never authorize risk
    evidence, a demo authorization, or execution.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(path).strip() in {"", ":memory:"}:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_DURABLE_PATH_REQUIRED")
        self._initialize()

    def register(
        self, record: SentinelPortfolioRiskPolicyRecord
    ) -> SentinelPortfolioRiskPolicyRecord:
        self._require_local(record)
        if record.lifecycle is not SentinelPolicyLifecycle.ACTIVE or record.lifecycle_version != 1:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_INITIAL_STATE_INVALID")
        with self._transaction() as connection:
            existing = self._row(connection, record.policy.policy_id, record.policy.policy_version)
            if existing is not None:
                restored = self._record(existing)
                if restored.record_fingerprint == record.record_fingerprint:
                    return restored
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_VERSION_CONFLICT")
            try:
                self._insert_record(connection, record)
                self._insert_event(connection, record, None, "POLICY_REGISTERED")
            except sqlite3.IntegrityError as error:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_ACTIVE_VERSION_CONFLICT") from error
        return record

    def resolve(self, *, policy_id: str, policy_version: str) -> SentinelPortfolioRiskPolicyRecord | None:
        with self._connection() as connection:
            row = self._row(connection, policy_id, policy_version)
        return None if row is None else self._record(row)

    def revoke(
        self, *, policy_id: str, policy_version: str, reason: str, changed_at: datetime
    ) -> SentinelPortfolioRiskPolicyRecord:
        now = _utc(changed_at)
        if not reason.strip():
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_REVOCATION_REASON_REQUIRED")
        with self._transaction() as connection:
            current = self._required(connection, policy_id, policy_version)
            if current.lifecycle is SentinelPolicyLifecycle.REVOKED:
                return current
            if current.lifecycle is not SentinelPolicyLifecycle.ACTIVE:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_TERMINAL_STATE")
            updated = _replace_record(
                current,
                {
                    "lifecycle": SentinelPolicyLifecycle.REVOKED,
                    "lifecycle_version": current.lifecycle_version + 1,
                    "lifecycle_changed_at": now,
                    "revoked_reason": reason.strip(),
                }
            )
            self._update_record(connection, updated)
            self._insert_event(connection, updated, current.lifecycle, "POLICY_REVOKED")
        return updated

    def supersede(
        self,
        *,
        current_policy_id: str,
        current_policy_version: str,
        replacement: SentinelPortfolioRiskPolicyRecord,
        changed_at: datetime,
    ) -> tuple[SentinelPortfolioRiskPolicyRecord, SentinelPortfolioRiskPolicyRecord]:
        self._require_local(replacement)
        now = _utc(changed_at)
        with self._transaction() as connection:
            current = self._required(connection, current_policy_id, current_policy_version)
            if current.lifecycle is not SentinelPolicyLifecycle.ACTIVE:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_TERMINAL_STATE")
            if replacement.lifecycle is not SentinelPolicyLifecycle.ACTIVE or replacement.lifecycle_version != 1:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_REPLACEMENT_INVALID")
            if replacement.policy.policy_id != current.policy.policy_id:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_POLICY_ID_MISMATCH")
            if replacement.supersedes_version != current.policy.policy_version:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_REPLACEMENT_LINEAGE_MISMATCH")
            ended = _replace_record(
                current,
                {
                    "lifecycle": SentinelPolicyLifecycle.SUPERSEDED,
                    "lifecycle_version": current.lifecycle_version + 1,
                    "lifecycle_changed_at": now,
                    "superseded_by_version": replacement.policy.policy_version,
                }
            )
            self._update_record(connection, ended)
            self._insert_event(connection, ended, current.lifecycle, "POLICY_SUPERSEDED")
            try:
                self._insert_record(connection, replacement)
                self._insert_event(connection, replacement, None, "POLICY_REGISTERED")
            except sqlite3.IntegrityError as error:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_VERSION_CONFLICT") from error
        return ended, replacement

    def history(self, *, policy_id: str, policy_version: str) -> tuple[tuple[object, ...], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event_id, lifecycle_version, from_state, to_state, reason_code,
                occurred_at, record_fingerprint FROM sentinel_policy_events
                WHERE policy_id = ? AND policy_version = ?
                ORDER BY lifecycle_version, event_id""",
                (policy_id, policy_version),
            ).fetchall()
        return tuple(rows)

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._transaction() as connection:
                existing_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if not existing_tables:
                    for statement in (*_TABLE_SQL, *_INDEX_SQL):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO sentinel_policy_metadata (key, value) VALUES ('schema_version', ?)",
                        (_SCHEMA_VERSION,),
                    )
                self._verify_schema(connection)
                existing = connection.execute(
                    "SELECT value FROM sentinel_policy_metadata WHERE key = 'schema_version'"
                ).fetchone()
                if existing is None:
                    raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_SCHEMA_INCOMPLETE")
                if existing[0] != _SCHEMA_VERSION:
                    raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_SCHEMA_UNSUPPORTED")
        except (sqlite3.DatabaseError, OSError) as error:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_UNAVAILABLE") from error

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        expected_tables = {
            _object_name(statement, "table"): _normalize_sql(statement)
            for statement in _TABLE_SQL
        }
        actual_tables = {
            row[0]: _normalize_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_tables != expected_tables:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_SCHEMA_INVALID")
        expected_indexes = {
            _object_name(statement, "index"): _normalize_sql(statement)
            for statement in _INDEX_SQL
        }
        actual_indexes = {
            row[0]: _normalize_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        }
        if actual_indexes != expected_indexes:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_SCHEMA_INVALID")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_INTEGRITY_INVALID")

    @staticmethod
    def _require_local(record: SentinelPortfolioRiskPolicyRecord) -> None:
        if record.authority_level is not SentinelPolicyAuthorityLevel.LOCAL_GOVERNANCE_ONLY:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_NON_LOCAL_RECORD")

    @staticmethod
    def _payload(record: SentinelPortfolioRiskPolicyRecord) -> str:
        return record.model_dump_json(exclude_computed_fields=True)

    def _insert_record(self, connection: sqlite3.Connection, record: SentinelPortfolioRiskPolicyRecord) -> None:
        connection.execute(
            """INSERT INTO sentinel_policy_records (
            policy_id, policy_version, lifecycle, lifecycle_version, content_fingerprint,
            record_fingerprint, record_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.policy.policy_id, record.policy.policy_version, record.lifecycle.value,
                record.lifecycle_version, record.policy_content_fingerprint,
                record.record_fingerprint, self._payload(record), record.lifecycle_changed_at.isoformat(),
            ),
        )

    def _update_record(self, connection: sqlite3.Connection, record: SentinelPortfolioRiskPolicyRecord) -> None:
        changed = connection.execute(
            """UPDATE sentinel_policy_records SET lifecycle = ?, lifecycle_version = ?,
            content_fingerprint = ?, record_fingerprint = ?, record_json = ?, updated_at = ?
            WHERE policy_id = ? AND policy_version = ?""",
            (
                record.lifecycle.value, record.lifecycle_version, record.policy_content_fingerprint,
                record.record_fingerprint, self._payload(record), record.lifecycle_changed_at.isoformat(),
                record.policy.policy_id, record.policy.policy_version,
            ),
        ).rowcount
        if changed != 1:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_UPDATE_FAILED")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        record: SentinelPortfolioRiskPolicyRecord,
        from_state: SentinelPolicyLifecycle | None,
        reason: str,
    ) -> None:
        payload = {
            "policy_id": record.policy.policy_id,
            "policy_version": record.policy.policy_version,
            "lifecycle_version": record.lifecycle_version,
            "from_state": None if from_state is None else from_state.value,
            "to_state": record.lifecycle.value,
            "reason_code": reason,
            "occurred_at": record.lifecycle_changed_at.isoformat(),
            "record_fingerprint": record.record_fingerprint,
        }
        event_id = "sentinel-policy-event-" + sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        connection.execute(
            """INSERT INTO sentinel_policy_events (
            event_id, policy_id, policy_version, lifecycle_version, from_state,
            to_state, reason_code, occurred_at, record_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, record.policy.policy_id, record.policy.policy_version,
                record.lifecycle_version, payload["from_state"], record.lifecycle.value,
                reason, record.lifecycle_changed_at.isoformat(), record.record_fingerprint,
            ),
        )

    @staticmethod
    def _row(connection: sqlite3.Connection, policy_id: str, policy_version: str):
        return connection.execute(
            "SELECT record_json, content_fingerprint, record_fingerprint FROM sentinel_policy_records WHERE policy_id = ? AND policy_version = ?",
            (policy_id, policy_version),
        ).fetchone()

    def _required(self, connection: sqlite3.Connection, policy_id: str, policy_version: str) -> SentinelPortfolioRiskPolicyRecord:
        row = self._row(connection, policy_id, policy_version)
        if row is None:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_VERSION_UNKNOWN")
        return self._record(row)

    @staticmethod
    def _record(row) -> SentinelPortfolioRiskPolicyRecord:
        try:
            record = SentinelPortfolioRiskPolicyRecord.model_validate_json(row[0])
        except Exception as error:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_RECORD_INVALID") from error
        if record.policy_content_fingerprint != row[1] or record.record_fingerprint != row[2]:
            raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_RECORD_TAMPERED")
        return record

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise SentinelPolicyCatalogError("SENTINEL_POLICY_CATALOG_FOREIGN_KEYS_UNAVAILABLE")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy catalog timestamps require timezone awareness")
    return value.astimezone(UTC)


def _replace_record(
    record: SentinelPortfolioRiskPolicyRecord, updates: dict[str, object]
) -> SentinelPortfolioRiskPolicyRecord:
    payload = record.model_dump(mode="python", exclude_computed_fields=True)
    payload.update(updates)
    return SentinelPortfolioRiskPolicyRecord.model_validate(payload)


def _normalize_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).rstrip(";").lower()


def _object_name(statement: str, kind: str) -> str:
    match = re.match(rf"\s*create\s+(?:unique\s+)?{kind}\s+([a-z0-9_]+)", statement, re.I)
    if match is None:
        raise ValueError("invalid canonical policy catalog DDL")
    return match.group(1)
