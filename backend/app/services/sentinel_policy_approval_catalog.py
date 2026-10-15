"""Durable local policy-approval lifecycle catalog without trust authority.

This store preserves immutable approval claims and their revocation or
supersession history.  A row proves only that local software recorded the
claim; it does not establish the approver's identity, authorization, signature,
or trust anchor and therefore cannot enable production policy resolution.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Iterator

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.sentinel_policy_approval import (
    SentinelPolicyApprovalLifecycle,
    SentinelPolicyApprovalSourceResult,
    SentinelPolicyApprovalSourceStatus,
    SentinelPolicyApprovalState,
)
from app.services.sentinel_policy_approval import approval_binding_codes


_SCHEMA_VERSION = "1"
_TABLE_SQL = (
    "CREATE TABLE sentinel_policy_approval_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE sentinel_policy_approval_records (
        artifact_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        policy_fingerprint TEXT NOT NULL,
        approver_id TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active','revoked','superseded')),
        lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
        state_fingerprint TEXT NOT NULL,
        state_json TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE sentinel_policy_approval_events (
        event_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
        from_state TEXT,
        to_state TEXT NOT NULL CHECK (to_state IN ('active','revoked','superseded')),
        reason_code TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        state_fingerprint TEXT NOT NULL,
        UNIQUE (artifact_id, lifecycle_version),
        FOREIGN KEY (artifact_id) REFERENCES sentinel_policy_approval_records(artifact_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)
_INDEX_SQL = (
    """CREATE UNIQUE INDEX sentinel_policy_approval_one_active
        ON sentinel_policy_approval_records (policy_id, policy_version)
        WHERE lifecycle = 'active'""",
    """CREATE INDEX sentinel_policy_approval_policy_lookup
        ON sentinel_policy_approval_records (policy_id, policy_version, artifact_id)""",
)


class SentinelPolicyApprovalCatalogError(RuntimeError):
    """The local approval lifecycle catalog cannot prove a safe result."""


class LocalSentinelPolicyApprovalCatalog:
    """Restart-safe, non-authoritative approval and revocation history."""

    source_id = "local_non_authoritative_sentinel_policy_approval_catalog"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = RLock()
        if str(path).strip() in {"", ":memory:"}:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_DURABLE_PATH_REQUIRED"
            )
        self._initialize()

    def register(
        self,
        state: SentinelPolicyApprovalState,
        *,
        policy: ControlledDemoExecutionPolicy,
    ) -> SentinelPolicyApprovalState:
        """Record an active claim after exact structural policy binding checks."""

        if (
            state.lifecycle is not SentinelPolicyApprovalLifecycle.ACTIVE
            or state.lifecycle_version != 1
            or state.artifact.supersedes_approval_artifact_id is not None
        ):
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_INITIAL_STATE_INVALID"
            )
        codes = approval_binding_codes(
            approval=state,
            policy=policy,
            evaluated_at=state.artifact.effective_at,
        )
        if codes:
            raise SentinelPolicyApprovalCatalogError(codes[0])
        artifact_id = state.artifact.approval_artifact_id
        with self._lock, self._transaction() as connection:
            existing = self._row(connection, artifact_id)
            if existing is not None:
                restored = self._state(existing)
                if restored.state_fingerprint == state.state_fingerprint:
                    return restored
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_ARTIFACT_CONFLICT"
                )
            try:
                self._insert_state(connection, state)
                self._insert_event(
                    connection,
                    state=state,
                    from_state=None,
                    reason_code="POLICY_APPROVAL_RECORDED_NON_AUTHORITATIVELY",
                )
            except sqlite3.IntegrityError as error:
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_ACTIVE_CONFLICT"
                ) from error
        return state

    def resolve(
        self,
        *,
        policy_id: str,
        policy_version: str,
        artifact_id: str,
        evaluated_at: datetime,
    ) -> SentinelPolicyApprovalSourceResult:
        """Return recorded lifecycle state without claiming independent authority."""

        now = _utc(evaluated_at)
        try:
            with self._connection() as connection:
                row = self._row(connection, artifact_id)
            if row is None:
                return SentinelPolicyApprovalSourceResult(
                    status=SentinelPolicyApprovalSourceStatus.NOT_FOUND,
                    source_id=self.source_id,
                    requested_policy_id=policy_id,
                    requested_policy_version=policy_version,
                    requested_artifact_id=artifact_id,
                    resolved_at=now,
                    reason_codes=("SENTINEL_POLICY_APPROVAL_NOT_RECORDED",),
                )
            state = self._state(row)
            artifact = state.artifact
            if (
                artifact.policy_id != policy_id.strip().upper()
                or artifact.policy_version != policy_version.strip().upper()
            ):
                return SentinelPolicyApprovalSourceResult(
                    status=SentinelPolicyApprovalSourceStatus.CONFLICT,
                    source_id=self.source_id,
                    requested_policy_id=policy_id,
                    requested_policy_version=policy_version,
                    requested_artifact_id=artifact_id,
                    resolved_at=now,
                    reason_codes=("SENTINEL_POLICY_APPROVAL_SOURCE_CONFLICT",),
                )
            return SentinelPolicyApprovalSourceResult(
                status=SentinelPolicyApprovalSourceStatus.AVAILABLE,
                source_id=self.source_id,
                requested_policy_id=policy_id,
                requested_policy_version=policy_version,
                requested_artifact_id=artifact_id,
                resolved_at=now,
                state=state,
            )
        except SentinelPolicyApprovalCatalogError:
            raise
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_UNAVAILABLE"
            ) from error

    def revoke(
        self,
        *,
        artifact_id: str,
        reason: str,
        changed_at: datetime,
    ) -> SentinelPolicyApprovalState:
        now = _utc(changed_at)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_REVOCATION_REASON_REQUIRED"
            )
        with self._lock, self._transaction() as connection:
            current = self._required(connection, artifact_id)
            if current.lifecycle is SentinelPolicyApprovalLifecycle.REVOKED:
                if current.revoked_reason == normalized_reason:
                    return current
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_REVOCATION_CONFLICT"
                )
            if current.lifecycle is not SentinelPolicyApprovalLifecycle.ACTIVE:
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_TERMINAL_STATE"
                )
            updated = _replace_state(
                current,
                {
                    "lifecycle": SentinelPolicyApprovalLifecycle.REVOKED,
                    "lifecycle_version": current.lifecycle_version + 1,
                    "lifecycle_changed_at": now,
                    "revoked_reason": normalized_reason,
                },
            )
            self._update_state(connection, updated, expected=current)
            self._insert_event(
                connection,
                state=updated,
                from_state=current.lifecycle,
                reason_code="POLICY_APPROVAL_REVOKED",
            )
        return updated

    def supersede(
        self,
        *,
        current_artifact_id: str,
        replacement: SentinelPolicyApprovalState,
        policy: ControlledDemoExecutionPolicy,
        changed_at: datetime,
    ) -> tuple[SentinelPolicyApprovalState, SentinelPolicyApprovalState]:
        now = _utc(changed_at)
        if (
            replacement.lifecycle is not SentinelPolicyApprovalLifecycle.ACTIVE
            or replacement.lifecycle_version != 1
            or replacement.artifact.supersedes_approval_artifact_id
            != current_artifact_id
        ):
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_REPLACEMENT_INVALID"
            )
        codes = approval_binding_codes(
            approval=replacement,
            policy=policy,
            evaluated_at=replacement.artifact.effective_at,
        )
        if codes:
            raise SentinelPolicyApprovalCatalogError(codes[0])
        with self._lock, self._transaction() as connection:
            current = self._required(connection, current_artifact_id)
            if current.lifecycle is not SentinelPolicyApprovalLifecycle.ACTIVE:
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_TERMINAL_STATE"
                )
            if (
                current.artifact.policy_id != replacement.artifact.policy_id
                or current.artifact.policy_version
                != replacement.artifact.policy_version
                or current.artifact.policy_fingerprint
                != replacement.artifact.policy_fingerprint
            ):
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_REPLACEMENT_LINEAGE_MISMATCH"
                )
            ended = _replace_state(
                current,
                {
                    "lifecycle": SentinelPolicyApprovalLifecycle.SUPERSEDED,
                    "lifecycle_version": current.lifecycle_version + 1,
                    "lifecycle_changed_at": now,
                    "superseded_by_artifact_id": (
                        replacement.artifact.approval_artifact_id
                    ),
                },
            )
            self._update_state(connection, ended, expected=current)
            self._insert_event(
                connection,
                state=ended,
                from_state=current.lifecycle,
                reason_code="POLICY_APPROVAL_SUPERSEDED",
            )
            try:
                self._insert_state(connection, replacement)
                self._insert_event(
                    connection,
                    state=replacement,
                    from_state=None,
                    reason_code="POLICY_APPROVAL_REPLACEMENT_RECORDED",
                )
            except sqlite3.IntegrityError as error:
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_REPLACEMENT_CONFLICT"
                ) from error
        return ended, replacement

    def history(self, *, artifact_id: str) -> tuple[tuple[object, ...], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT event_id, lifecycle_version, from_state, to_state, "
                "reason_code, occurred_at, state_fingerprint "
                "FROM sentinel_policy_approval_events WHERE artifact_id = ? "
                "ORDER BY lifecycle_version, event_id",
                (artifact_id,),
            ).fetchall()
        return tuple(rows)

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._transaction() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if not tables:
                    for statement in (*_TABLE_SQL, *_INDEX_SQL):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO sentinel_policy_approval_metadata "
                        "(key, value) VALUES ('schema_version', ?)",
                        (_SCHEMA_VERSION,),
                    )
                self._verify_schema(connection)
                version = connection.execute(
                    "SELECT value FROM sentinel_policy_approval_metadata "
                    "WHERE key = 'schema_version'"
                ).fetchone()
                if version is None:
                    raise SentinelPolicyApprovalCatalogError(
                        "SENTINEL_POLICY_APPROVAL_CATALOG_SCHEMA_INCOMPLETE"
                    )
                if version[0] != _SCHEMA_VERSION:
                    raise SentinelPolicyApprovalCatalogError(
                        "SENTINEL_POLICY_APPROVAL_CATALOG_SCHEMA_UNSUPPORTED"
                    )
        except SentinelPolicyApprovalCatalogError:
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_UNAVAILABLE"
            ) from error

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        expected_tables = {
            _object_name(statement, "table"): _normalize_sql(statement)
            for statement in _TABLE_SQL
        }
        actual_tables = {
            str(row[0]): _normalize_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_indexes = {
            _object_name(statement, "index"): _normalize_sql(statement)
            for statement in _INDEX_SQL
        }
        actual_indexes = {
            str(row[0]): _normalize_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        }
        if actual_tables != expected_tables or actual_indexes != expected_indexes:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_SCHEMA_INVALID"
            )
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_INTEGRITY_INVALID"
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_INTEGRITY_INVALID"
            )

    @staticmethod
    def _insert_state(
        connection: sqlite3.Connection, state: SentinelPolicyApprovalState
    ) -> None:
        artifact = state.artifact
        connection.execute(
            "INSERT INTO sentinel_policy_approval_records "
            "(artifact_id, policy_id, policy_version, policy_fingerprint, "
            "approver_id, lifecycle, lifecycle_version, state_fingerprint, "
            "state_json, registered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.approval_artifact_id,
                artifact.policy_id,
                artifact.policy_version,
                artifact.policy_fingerprint,
                artifact.approver_id,
                state.lifecycle.value,
                state.lifecycle_version,
                state.state_fingerprint,
                state.model_dump_json(exclude_computed_fields=True),
                state.registered_at.isoformat(),
                state.lifecycle_changed_at.isoformat(),
            ),
        )

    @staticmethod
    def _update_state(
        connection: sqlite3.Connection,
        state: SentinelPolicyApprovalState,
        *,
        expected: SentinelPolicyApprovalState,
    ) -> None:
        changed = connection.execute(
            "UPDATE sentinel_policy_approval_records SET lifecycle = ?, "
            "lifecycle_version = ?, state_fingerprint = ?, state_json = ?, "
            "updated_at = ? WHERE artifact_id = ? AND lifecycle = ? "
            "AND lifecycle_version = ? AND state_fingerprint = ?",
            (
                state.lifecycle.value,
                state.lifecycle_version,
                state.state_fingerprint,
                state.model_dump_json(exclude_computed_fields=True),
                state.lifecycle_changed_at.isoformat(),
                state.artifact.approval_artifact_id,
                expected.lifecycle.value,
                expected.lifecycle_version,
                expected.state_fingerprint,
            ),
        ).rowcount
        if changed != 1:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_TRANSITION_CONFLICT"
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        state: SentinelPolicyApprovalState,
        from_state: SentinelPolicyApprovalLifecycle | None,
        reason_code: str,
    ) -> None:
        payload = {
            "artifact_id": state.artifact.approval_artifact_id,
            "lifecycle_version": state.lifecycle_version,
            "from_state": None if from_state is None else from_state.value,
            "to_state": state.lifecycle.value,
            "reason_code": reason_code,
            "state_fingerprint": state.state_fingerprint,
        }
        event_id = "sentinel-policy-approval-event-" + _fingerprint(payload)
        connection.execute(
            "INSERT INTO sentinel_policy_approval_events "
            "(event_id, artifact_id, lifecycle_version, from_state, to_state, "
            "reason_code, occurred_at, state_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                state.artifact.approval_artifact_id,
                state.lifecycle_version,
                payload["from_state"],
                state.lifecycle.value,
                reason_code,
                state.lifecycle_changed_at.isoformat(),
                state.state_fingerprint,
            ),
        )

    @staticmethod
    def _row(connection: sqlite3.Connection, artifact_id: str):
        return connection.execute(
            "SELECT state_json, state_fingerprint, policy_id, policy_version, "
            "policy_fingerprint, approver_id, lifecycle, lifecycle_version "
            "FROM sentinel_policy_approval_records WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()

    def _required(
        self, connection: sqlite3.Connection, artifact_id: str
    ) -> SentinelPolicyApprovalState:
        row = self._row(connection, artifact_id)
        if row is None:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_ARTIFACT_UNKNOWN"
            )
        return self._state(row)

    @staticmethod
    def _state(row) -> SentinelPolicyApprovalState:
        try:
            state = SentinelPolicyApprovalState.model_validate_json(row[0])
        except Exception as error:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_RECORD_INVALID"
            ) from error
        artifact = state.artifact
        expected = (
            state.state_fingerprint,
            artifact.policy_id,
            artifact.policy_version,
            artifact.policy_fingerprint,
            artifact.approver_id,
            state.lifecycle.value,
            state.lifecycle_version,
        )
        if tuple(row[1:]) != expected:
            raise SentinelPolicyApprovalCatalogError(
                "SENTINEL_POLICY_APPROVAL_CATALOG_RECORD_TAMPERED"
            )
        return state

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise SentinelPolicyApprovalCatalogError(
                    "SENTINEL_POLICY_APPROVAL_CATALOG_FOREIGN_KEYS_UNAVAILABLE"
                )
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                self._commit(connection)
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _commit(connection: sqlite3.Connection) -> None:
        connection.commit()


def _replace_state(
    state: SentinelPolicyApprovalState, updates: dict[str, object]
) -> SentinelPolicyApprovalState:
    payload = state.model_dump(mode="python", exclude_computed_fields=True)
    payload.update(updates)
    return SentinelPolicyApprovalState.model_validate(payload)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("policy approval catalog timestamps require timezone awareness")
    return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode()
    ).hexdigest()


def _normalize_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).rstrip(";").lower()


def _object_name(statement: str, kind: str) -> str:
    match = re.match(
        rf"\s*create\s+(?:unique\s+)?{kind}\s+([a-z0-9_]+)", statement, re.I
    )
    if match is None:
        raise ValueError("invalid canonical policy approval catalog DDL")
    return match.group(1)
