"""Small SQLite store for validated instrument catalogue snapshots only."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from app.models.instruments import (
    ExchangeResolution,
    InstrumentAlias,
    InstrumentCatalogueEntry,
    InstrumentCategory,
    InstrumentContractKind,
    InstrumentProviderMapping,
    InstrumentSourceProvenance,
    canonical_instrument_id,
)


_SCHEMA_VERSION = "2"
_LEGACY_SCHEMA_VERSION = "1"
_MIGRATION_STATE_READY = "READY"
_MIGRATION_STATE_MIGRATING = "MIGRATING"
_V1_SCHEMA_SQL = (
    """CREATE TABLE catalogue_metadata (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE catalogue_instruments (
        instrument_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        category TEXT NOT NULL,
        provider_symbol TEXT NOT NULL,
        display_symbol TEXT NOT NULL,
        display_name TEXT,
        base_currency TEXT,
        quote_currency TEXT,
        forex_group TEXT,
        commodity_category TEXT,
        description TEXT,
        available_exchanges_json TEXT NOT NULL,
        quote_access TEXT NOT NULL
    )""",
    """CREATE INDEX catalogue_instruments_lookup
        ON catalogue_instruments (category, forex_group, provider_symbol)""",
)
_V2_SCHEMA_SQL = (
    """CREATE TABLE catalogue_metadata (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE catalogue_instruments (
        instrument_id TEXT PRIMARY KEY,
        canonical_instrument_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL,
        category TEXT NOT NULL,
        provider_symbol TEXT NOT NULL,
        canonical_symbol TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        exchange_resolution TEXT NOT NULL CHECK (
            exchange_resolution IN ('resolved', 'venue_neutral', 'legacy_unresolved')
        ),
        exchange_name TEXT,
        contract_kind TEXT NOT NULL CHECK (
            contract_kind IN ('none', 'dated', 'continuous')
        ),
        contract_expiry TEXT,
        continuous_contract_rank INTEGER,
        display_symbol TEXT NOT NULL,
        display_name TEXT,
        base_currency TEXT,
        quote_currency TEXT,
        forex_group TEXT,
        commodity_category TEXT,
        description TEXT,
        available_exchanges_json TEXT NOT NULL,
        quote_access TEXT NOT NULL CHECK (quote_access = 'unknown'),
        historical_data_access TEXT NOT NULL CHECK (historical_data_access = 'unknown'),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE catalogue_provider_mappings (
        instrument_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        provider_symbol TEXT NOT NULL,
        provider_exchange_id TEXT NOT NULL,
        provider_contract_id TEXT NOT NULL,
        provider_instrument_id TEXT,
        PRIMARY KEY (
            instrument_id, source_id, provider_symbol, provider_exchange_id,
            provider_contract_id
        ),
        UNIQUE (
            source_id, provider_symbol, provider_exchange_id, provider_contract_id
        ),
        FOREIGN KEY (instrument_id) REFERENCES catalogue_instruments(instrument_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )""",
    """CREATE TABLE catalogue_aliases (
        instrument_id TEXT NOT NULL,
        alias TEXT NOT NULL,
        alias_type TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        PRIMARY KEY (instrument_id, alias, alias_type, exchange_id, source_id),
        FOREIGN KEY (instrument_id) REFERENCES catalogue_instruments(instrument_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )""",
    """CREATE TABLE catalogue_provenance (
        instrument_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (instrument_id, source_id, source_record_id, observed_at),
        FOREIGN KEY (instrument_id) REFERENCES catalogue_instruments(instrument_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )""",
    """CREATE INDEX catalogue_instruments_lookup
        ON catalogue_instruments (category, exchange_id, provider_symbol)""",
    """CREATE INDEX catalogue_aliases_lookup
        ON catalogue_aliases (alias, exchange_id)""",
    """CREATE INDEX catalogue_provenance_source_lookup
        ON catalogue_provenance (source_id, source_record_id)""",
)


class InstrumentCatalogueSchemaError(RuntimeError):
    """The catalogue database cannot be safely recognized or initialized."""


@dataclass(frozen=True)
class StoredCatalogueStatus:
    refreshed_at: datetime | None
    last_refresh_attempt_at: datetime | None
    last_refresh_error: str | None
    instrument_count: int
    last_refresh_error_category: str | None = None
    last_refresh_error_status_code: int | None = None
    last_refresh_error_provider_code: str | None = None

    @property
    def available(self) -> bool:
        return self.instrument_count > 0


class InstrumentCatalogueStore:
    """Dedicated persistent cache, intentionally unrelated to execution state."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def status(self) -> StoredCatalogueStatus:
        with self._connect() as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM catalogue_metadata")
            }
            count = int(
                connection.execute("SELECT COUNT(*) FROM catalogue_instruments").fetchone()[0]
            )
        return StoredCatalogueStatus(
            refreshed_at=_parse_datetime(values.get("refreshed_at")),
            last_refresh_attempt_at=_parse_datetime(values.get("last_refresh_attempt_at")),
            last_refresh_error=values.get("last_refresh_error"),
            last_refresh_error_category=values.get("last_refresh_error_category"),
            last_refresh_error_status_code=_parse_int(values.get("last_refresh_error_status_code")),
            last_refresh_error_provider_code=values.get("last_refresh_error_provider_code"),
            instrument_count=count,
        )

    def mark_refresh_attempt(self, attempted_at: datetime) -> None:
        self._set_metadata("last_refresh_attempt_at", _serialize_datetime(attempted_at))

    def mark_refresh_failure(
        self,
        reason: str,
        *,
        category: str | None = None,
        status_code: int | None = None,
        provider_code: str | None = None,
    ) -> None:
        self._set_metadata("last_refresh_error", reason)
        self._set_metadata("last_refresh_error_category", category)
        self._set_metadata(
            "last_refresh_error_status_code",
            str(status_code) if status_code is not None else None,
        )
        self._set_metadata("last_refresh_error_provider_code", provider_code)

    def replace_snapshot(
        self,
        instruments: tuple[InstrumentCatalogueEntry, ...],
        *,
        refreshed_at: datetime,
    ) -> None:
        if not instruments:
            raise ValueError("cannot persist an empty instrument catalogue snapshot")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_created_at = {
                    row["canonical_instrument_id"]: row["created_at"]
                    for row in connection.execute(
                        "SELECT canonical_instrument_id, created_at "
                        "FROM catalogue_instruments"
                    )
                }
                connection.execute("DELETE FROM catalogue_provider_mappings")
                connection.execute("DELETE FROM catalogue_aliases")
                connection.execute("DELETE FROM catalogue_provenance")
                connection.execute("DELETE FROM catalogue_instruments")
                for entry in instruments:
                    created_at = _parse_datetime(
                        existing_created_at.get(entry.canonical_instrument_id)
                    ) or entry.created_at
                    updated_at = max(refreshed_at, created_at)
                    _insert_entry(
                        connection,
                        entry,
                        created_at=created_at,
                        updated_at=updated_at,
                    )
                self._upsert_metadata(connection, "refreshed_at", _serialize_datetime(refreshed_at))
                self._upsert_metadata(connection, "last_refresh_attempt_at", _serialize_datetime(refreshed_at))
                self._upsert_metadata(connection, "last_refresh_error", None)
                self._upsert_metadata(connection, "last_refresh_error_category", None)
                self._upsert_metadata(connection, "last_refresh_error_status_code", None)
                self._upsert_metadata(connection, "last_refresh_error_provider_code", None)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def search(
        self,
        *,
        query: str | None,
        category: InstrumentCategory | None,
        forex_group: str | None,
        page: int,
        limit: int,
    ) -> tuple[int, tuple[InstrumentCatalogueEntry, ...]]:
        clauses: list[str] = []
        params: list[object] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category.value)
        if forex_group is not None:
            clauses.append("forex_group = ?")
            params.append(forex_group)
        if query:
            escaped = _escape_like(query.lower())
            clauses.append(
                "(LOWER(provider_symbol) LIKE ? ESCAPE '\\' "
                "OR LOWER(canonical_symbol) LIKE ? ESCAPE '\\' "
                "OR LOWER(display_symbol) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(display_name, '')) LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM catalogue_aliases "
                "WHERE catalogue_aliases.instrument_id = catalogue_instruments.instrument_id "
                "AND LOWER(catalogue_aliases.alias) LIKE ? ESCAPE '\\'))"
            )
            params.extend([f"%{escaped}%"] * 5)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM catalogue_instruments{where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM catalogue_instruments{where}
                ORDER BY category, provider_symbol, instrument_id
                LIMIT ? OFFSET ?
                """,
                [*params, limit, (page - 1) * limit],
            ).fetchall()
            entries = tuple(_entry_from_row(connection, row) for row in rows)
        return count, entries

    def get(self, instrument_id: str) -> InstrumentCatalogueEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalogue_instruments "
                "WHERE instrument_id = ? OR canonical_instrument_id = ?",
                (instrument_id, instrument_id),
            ).fetchone()
            return _entry_from_row(connection, row) if row is not None else None

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            signature = _schema_signature(connection)
            if not signature:
                _create_schema(connection, schema_version=_SCHEMA_VERSION)
                self._write_schema_metadata(
                    connection, schema_version=_SCHEMA_VERSION
                )
            elif signature == _canonical_schema_signature(_LEGACY_SCHEMA_VERSION):
                metadata = self._schema_metadata(connection)
                version_present = "schema_version" in metadata
                state_present = "migration_state" in metadata
                if not version_present and not state_present:
                    self._write_schema_metadata(
                        connection, schema_version=_LEGACY_SCHEMA_VERSION
                    )
                elif not version_present or not state_present:
                    raise InstrumentCatalogueSchemaError(
                        "catalogue schema metadata is incomplete"
                    )
                if self._schema_metadata(connection) != {
                    "schema_version": _LEGACY_SCHEMA_VERSION,
                    "migration_state": _MIGRATION_STATE_READY,
                }:
                    raise InstrumentCatalogueSchemaError(
                        "catalogue schema version or migration state is unsupported"
                    )
                self._apply_v2_migration(connection)
            elif signature == _canonical_schema_signature(_SCHEMA_VERSION):
                metadata = self._schema_metadata(connection)
                if "schema_version" not in metadata or "migration_state" not in metadata:
                    raise InstrumentCatalogueSchemaError(
                        "catalogue schema metadata is incomplete"
                    )
            else:
                raise InstrumentCatalogueSchemaError("catalogue schema is invalid")

            self._verify_ready_schema(connection)
            self._commit(connection)
        except BaseException:
            try:
                connection.rollback()
            except BaseException:
                # Cleanup failure must not replace the initialization failure.
                pass
            try:
                connection.close()
            except BaseException:
                pass
            raise
        else:
            connection.close()

    @staticmethod
    def _commit(connection: sqlite3.Connection) -> None:
        connection.commit()

    @staticmethod
    def _write_schema_metadata(
        connection: sqlite3.Connection, *, schema_version: str
    ) -> None:
        connection.executemany(
            "INSERT INTO catalogue_metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", schema_version),
                ("migration_state", _MIGRATION_STATE_READY),
            ),
        )

    @staticmethod
    def _schema_metadata(connection: sqlite3.Connection) -> dict[str, str | None]:
        return {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key, value FROM catalogue_metadata "
                "WHERE key IN ('schema_version', 'migration_state')"
            )
        }

    @staticmethod
    def _verify_schema_structure(
        connection: sqlite3.Connection, *, schema_version: str
    ) -> None:
        if _schema_signature(connection) != _canonical_schema_signature(schema_version):
            raise InstrumentCatalogueSchemaError("catalogue schema is invalid")

    def _verify_ready_schema(self, connection: sqlite3.Connection) -> None:
        self._verify_schema_structure(connection, schema_version=_SCHEMA_VERSION)
        metadata = self._schema_metadata(connection)
        if metadata != {
            "schema_version": _SCHEMA_VERSION,
            "migration_state": _MIGRATION_STATE_READY,
        }:
            raise InstrumentCatalogueSchemaError(
                "catalogue schema version or migration state is unsupported"
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in integrity] != ["ok"]:
            raise InstrumentCatalogueSchemaError("catalogue database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise InstrumentCatalogueSchemaError("catalogue foreign key check failed")

    def _apply_v2_migration(self, connection: sqlite3.Connection) -> None:
        updated = connection.execute(
            "UPDATE catalogue_metadata SET value = ? "
            "WHERE key = 'migration_state' AND value = ?",
            (_MIGRATION_STATE_MIGRATING, _MIGRATION_STATE_READY),
        )
        if updated.rowcount != 1:
            raise InstrumentCatalogueSchemaError("catalogue migration state is invalid")

        metadata = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM catalogue_metadata")
        }
        try:
            migration_time = (
                _parse_datetime(metadata.get("refreshed_at")) or datetime.now(UTC)
            )
        except (TypeError, ValueError):
            raise InstrumentCatalogueSchemaError(
                "catalogue legacy timestamps are invalid"
            ) from None

        legacy_rows = connection.execute(
            "SELECT * FROM catalogue_instruments ORDER BY instrument_id"
        ).fetchall()
        connection.execute("DROP INDEX catalogue_instruments_lookup")
        connection.execute(
            "ALTER TABLE catalogue_instruments RENAME TO catalogue_instruments_v1"
        )
        _create_schema(
            connection,
            schema_version=_SCHEMA_VERSION,
            include_metadata=False,
        )
        for row in legacy_rows:
            entry = _entry_from_v1_row(row, migration_time=migration_time)
            _insert_entry(
                connection,
                entry,
                created_at=migration_time,
                updated_at=migration_time,
            )
        connection.execute("DROP TABLE catalogue_instruments_v1")
        connection.execute(
            "UPDATE catalogue_metadata SET value = ? WHERE key = 'schema_version'",
            (_SCHEMA_VERSION,),
        )
        connection.execute(
            "UPDATE catalogue_metadata SET value = ? WHERE key = 'migration_state'",
            (_MIGRATION_STATE_READY,),
        )

    def _set_metadata(self, key: str, value: str | None) -> None:
        with self._connect() as connection:
            self._upsert_metadata(connection, key, value)

    @staticmethod
    def _upsert_metadata(
        connection: sqlite3.Connection, key: str, value: str | None
    ) -> None:
        connection.execute(
            """
            INSERT INTO catalogue_metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise InstrumentCatalogueSchemaError(
                "catalogue foreign key enforcement is unavailable"
            )
        return connection


def _create_schema(
    connection: sqlite3.Connection,
    *,
    schema_version: str,
    include_metadata: bool = True,
) -> None:
    if schema_version == _LEGACY_SCHEMA_VERSION:
        statements = _V1_SCHEMA_SQL
    elif schema_version == _SCHEMA_VERSION:
        statements = _V2_SCHEMA_SQL
    else:
        raise InstrumentCatalogueSchemaError("catalogue schema version is unsupported")
    for index, statement in enumerate(statements):
        if not include_metadata and index == 0:
            continue
        connection.execute(statement)


def _schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            _normalize_sql(str(row[3])),
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )


@lru_cache(maxsize=2)
def _canonical_schema_signature(
    schema_version: str,
) -> tuple[tuple[str, str, str, str], ...]:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_schema(connection, schema_version=schema_version)
        return _schema_signature(connection)


def _normalize_sql(value: str) -> str:
    return " ".join(value.lower().split())


def _entry_row(
    entry: InstrumentCatalogueEntry,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> tuple[object, ...]:
    return (
        entry.instrument_id,
        entry.canonical_instrument_id,
        entry.source_id,
        entry.category.value,
        entry.provider_symbol,
        entry.canonical_symbol,
        entry.exchange_id,
        entry.exchange_resolution.value,
        entry.exchange_name,
        entry.contract_kind.value,
        entry.contract_expiry.isoformat() if entry.contract_expiry is not None else None,
        entry.continuous_contract_rank,
        entry.display_symbol,
        entry.display_name,
        entry.base_currency,
        entry.quote_currency,
        entry.forex_group,
        entry.commodity_category,
        entry.description,
        json.dumps(entry.available_exchanges),
        entry.quote_access,
        entry.historical_data_access,
        _serialize_datetime(created_at),
        _serialize_datetime(updated_at),
    )


def _insert_entry(
    connection: sqlite3.Connection,
    entry: InstrumentCatalogueEntry,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    connection.execute(
        """INSERT INTO catalogue_instruments (
            instrument_id, canonical_instrument_id, source_id, category,
            provider_symbol, canonical_symbol, exchange_id, exchange_resolution,
            exchange_name, contract_kind, contract_expiry, continuous_contract_rank,
            display_symbol, display_name, base_currency, quote_currency,
            forex_group, commodity_category, description,
            available_exchanges_json, quote_access, historical_data_access,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        _entry_row(entry, created_at=created_at, updated_at=updated_at),
    )
    connection.executemany(
        """INSERT INTO catalogue_provider_mappings (
            instrument_id, source_id, provider_symbol, provider_exchange_id,
            provider_contract_id, provider_instrument_id
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (
                entry.instrument_id,
                mapping.source_id,
                mapping.provider_symbol,
                mapping.provider_exchange_id,
                mapping.provider_contract_id,
                mapping.provider_instrument_id,
            )
            for mapping in entry.provider_mappings
        ],
    )
    connection.executemany(
        """INSERT INTO catalogue_aliases (
            instrument_id, alias, alias_type, exchange_id, source_id
        ) VALUES (?, ?, ?, ?, ?)""",
        [
            (
                entry.instrument_id,
                alias.alias,
                alias.alias_type,
                alias.exchange_id,
                alias.source_id,
            )
            for alias in entry.aliases
        ],
    )
    connection.executemany(
        """INSERT INTO catalogue_provenance (
            instrument_id, source_id, source_record_id, observed_at
        ) VALUES (?, ?, ?, ?)""",
        [
            (
                entry.instrument_id,
                provenance.source_id,
                provenance.source_record_id,
                _serialize_datetime(provenance.observed_at),
            )
            for provenance in entry.source_provenance
        ],
    )


def _entry_from_row(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> InstrumentCatalogueEntry:
    mappings = tuple(
        InstrumentProviderMapping(
            source_id=mapping["source_id"],
            provider_symbol=mapping["provider_symbol"],
            provider_exchange_id=mapping["provider_exchange_id"],
            provider_contract_id=mapping["provider_contract_id"],
            provider_instrument_id=mapping["provider_instrument_id"],
        )
        for mapping in connection.execute(
            "SELECT source_id, provider_symbol, provider_exchange_id, "
            "provider_contract_id, "
            "provider_instrument_id FROM catalogue_provider_mappings "
            "WHERE instrument_id = ? ORDER BY source_id, provider_symbol, "
            "provider_exchange_id, provider_contract_id",
            (row["instrument_id"],),
        )
    )
    aliases = tuple(
        InstrumentAlias(
            alias=alias["alias"],
            alias_type=alias["alias_type"],
            exchange_id=alias["exchange_id"],
            source_id=alias["source_id"],
        )
        for alias in connection.execute(
            "SELECT alias, alias_type, exchange_id, source_id "
            "FROM catalogue_aliases WHERE instrument_id = ? "
            "ORDER BY alias, alias_type, exchange_id, source_id",
            (row["instrument_id"],),
        )
    )
    provenance = tuple(
        InstrumentSourceProvenance(
            source_id=item["source_id"],
            source_record_id=item["source_record_id"],
            observed_at=_parse_required_datetime(item["observed_at"]),
        )
        for item in connection.execute(
            "SELECT source_id, source_record_id, observed_at "
            "FROM catalogue_provenance WHERE instrument_id = ? "
            "ORDER BY source_id, source_record_id, observed_at",
            (row["instrument_id"],),
        )
    )
    return InstrumentCatalogueEntry(
        instrument_id=row["instrument_id"],
        canonical_instrument_id=row["canonical_instrument_id"],
        source_id=row["source_id"],
        category=InstrumentCategory(row["category"]),
        provider_symbol=row["provider_symbol"],
        canonical_symbol=row["canonical_symbol"],
        exchange_id=row["exchange_id"],
        exchange_resolution=ExchangeResolution(row["exchange_resolution"]),
        exchange_name=row["exchange_name"],
        contract_kind=InstrumentContractKind(row["contract_kind"]),
        contract_expiry=_parse_optional_date(row["contract_expiry"]),
        continuous_contract_rank=row["continuous_contract_rank"],
        display_symbol=row["display_symbol"],
        display_name=row["display_name"],
        base_currency=row["base_currency"],
        quote_currency=row["quote_currency"],
        forex_group=row["forex_group"],
        commodity_category=row["commodity_category"],
        description=row["description"],
        available_exchanges=_parse_exchange_identifiers(
            row["available_exchanges_json"]
        ),
        provider_mappings=mappings,
        aliases=aliases,
        source_provenance=provenance,
        created_at=_parse_required_datetime(row["created_at"]),
        updated_at=_parse_required_datetime(row["updated_at"]),
        quote_access=row["quote_access"],
        historical_data_access=row["historical_data_access"],
    )


def _entry_from_v1_row(
    row: sqlite3.Row, *, migration_time: datetime
) -> InstrumentCatalogueEntry:
    try:
        category = InstrumentCategory(row["category"])
        provider_symbol = str(row["provider_symbol"])
        canonical_symbol = provider_symbol.upper()
        instrument_id = str(row["instrument_id"])
        exchange_id = _legacy_unresolved_exchange_id(instrument_id)
        display_symbol = str(row["display_symbol"])
        aliases = (
            (
                InstrumentAlias(
                    alias=display_symbol,
                    exchange_id=exchange_id,
                    source_id=str(row["source_id"]),
                ),
            )
            if display_symbol != provider_symbol
            else ()
        )
        available_exchanges = _parse_exchange_identifiers(
            row["available_exchanges_json"]
        )
        return InstrumentCatalogueEntry(
            instrument_id=instrument_id,
            canonical_instrument_id=canonical_instrument_id(
                category, exchange_id, canonical_symbol
            ),
            source_id=row["source_id"],
            category=category,
            provider_symbol=provider_symbol,
            canonical_symbol=canonical_symbol,
            exchange_id=exchange_id,
            exchange_resolution=ExchangeResolution.LEGACY_UNRESOLVED,
            exchange_name=None,
            contract_kind=InstrumentContractKind.NONE,
            display_symbol=display_symbol,
            display_name=row["display_name"],
            base_currency=row["base_currency"],
            quote_currency=row["quote_currency"],
            forex_group=row["forex_group"],
            commodity_category=row["commodity_category"],
            description=row["description"],
            available_exchanges=available_exchanges,
            provider_mappings=(
                InstrumentProviderMapping(
                    source_id=row["source_id"],
                    provider_symbol=provider_symbol,
                    provider_exchange_id=exchange_id,
                    provider_contract_id="NONE",
                    provider_instrument_id=instrument_id,
                ),
            ),
            aliases=aliases,
            source_provenance=(
                InstrumentSourceProvenance(
                    source_id=row["source_id"],
                    source_record_id=instrument_id,
                    observed_at=migration_time,
                ),
            ),
            created_at=migration_time,
            updated_at=migration_time,
            quote_access=row["quote_access"],
            historical_data_access="unknown",
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise InstrumentCatalogueSchemaError(
            "catalogue legacy record is invalid"
        ) from None


def _legacy_unresolved_exchange_id(instrument_id: str) -> str:
    fingerprint = sha256(instrument_id.encode("utf-8")).hexdigest()[:24]
    return f"LEGACY_UNRESOLVED:{fingerprint.upper()}"


def _serialize_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_required_datetime(value: str) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise InstrumentCatalogueSchemaError("catalogue timestamp is missing")
    return parsed


def _parse_optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InstrumentCatalogueSchemaError(
            "catalogue contract expiry is invalid"
        ) from None


def _parse_exchange_identifiers(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise InstrumentCatalogueSchemaError(
            "catalogue exchange identifiers are invalid"
        ) from None
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise InstrumentCatalogueSchemaError(
            "catalogue exchange identifiers are invalid"
        )
    return tuple(parsed)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
