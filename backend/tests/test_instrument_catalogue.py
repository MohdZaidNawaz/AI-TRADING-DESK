import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.routes.instruments import get_instrument_catalogue_service
from app.catalogue.store import (
    InstrumentCatalogueSchemaError,
    InstrumentCatalogueStore,
)
from app.main import app
from app.models.instruments import (
    ExchangeResolution,
    InstrumentAlias,
    InstrumentCatalogueEntry,
    InstrumentCataloguePage,
    InstrumentCategory,
    InstrumentContractKind,
    InstrumentProviderMapping,
    InstrumentSourceProvenance,
    canonical_instrument_id,
)
from app.services.instrument_catalogue import InstrumentCatalogueService


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _entry(
    category: InstrumentCategory,
    symbol: str,
    *,
    forex_group: str | None = None,
    exchanges: tuple[str, ...] = (),
    exchange_id: str = "GLOBAL",
    source_id: str = "twelve_data",
    contract_kind: InstrumentContractKind = InstrumentContractKind.NONE,
    contract_expiry: date | None = None,
    continuous_contract_rank: int | None = None,
) -> InstrumentCatalogueEntry:
    if category is InstrumentCategory.FUTURE and contract_kind is InstrumentContractKind.NONE:
        contract_kind = InstrumentContractKind.CONTINUOUS
        continuous_contract_rank = 1
    contract_identity = (
        f"dated:{contract_expiry.isoformat()}"
        if contract_kind is InstrumentContractKind.DATED and contract_expiry is not None
        else (
            f"continuous:{continuous_contract_rank}"
            if contract_kind is InstrumentContractKind.CONTINUOUS
            else "none"
        )
    )
    instrument_id = (
        f"{source_id}:{category.value}:{symbol}"
        if exchange_id == "GLOBAL" and contract_kind is InstrumentContractKind.NONE
        else f"{source_id}:{category.value}:{exchange_id}:{symbol}:{contract_identity}"
    )
    canonical_symbol = symbol.upper()
    display_symbol = symbol.replace("/", "")
    return InstrumentCatalogueEntry(
        instrument_id=instrument_id,
        canonical_instrument_id=canonical_instrument_id(
            category,
            exchange_id,
            canonical_symbol,
            contract_kind=contract_kind,
            contract_expiry=contract_expiry,
            continuous_contract_rank=continuous_contract_rank,
        ),
        source_id=source_id,
        category=category,
        provider_symbol=symbol,
        canonical_symbol=canonical_symbol,
        exchange_id=exchange_id,
        exchange_resolution=(
            ExchangeResolution.VENUE_NEUTRAL
            if exchange_id == "GLOBAL"
            else ExchangeResolution.RESOLVED
        ),
        contract_kind=contract_kind,
        contract_expiry=contract_expiry,
        continuous_contract_rank=continuous_contract_rank,
        display_symbol=display_symbol,
        display_name=f"{symbol} reference",
        forex_group=forex_group,
        available_exchanges=exchanges,
        provider_mappings=(
            InstrumentProviderMapping(
                source_id=source_id,
                provider_symbol=symbol,
                provider_exchange_id=exchange_id,
                provider_contract_id=contract_identity.upper(),
                provider_instrument_id=instrument_id,
            ),
        ),
        aliases=(
            (
                InstrumentAlias(
                    alias=display_symbol,
                    exchange_id=exchange_id,
                    source_id=source_id,
                ),
            )
            if display_symbol != symbol
            else ()
        ),
        source_provenance=(
            InstrumentSourceProvenance(
                source_id=source_id,
                source_record_id=instrument_id,
                observed_at=NOW,
            ),
        ),
        created_at=NOW,
        updated_at=NOW,
    )


class StubCatalogueClient:
    def __init__(self) -> None:
        self.calls: list[tuple[InstrumentCategory, int]] = []
        self.fail = False
        self.delay = False
        self.pages = {
            InstrumentCategory.FOREX: {
                1: (_entry(InstrumentCategory.FOREX, "EUR/USD", forex_group="Major"),),
                2: (_entry(InstrumentCategory.FOREX, "USD/INR", forex_group="Exotic"),),
            },
            InstrumentCategory.COMMODITY: {
                1: (_entry(InstrumentCategory.COMMODITY, "XAU/USD"),),
            },
            InstrumentCategory.CRYPTO: {
                1: (_entry(InstrumentCategory.CRYPTO, "BTC/USD", exchanges=("Binance", "Coinbase")),),
            },
        }

    async def fetch_forex_pairs(self, *, page: int, outputsize: int) -> InstrumentCataloguePage:
        return await self._page(InstrumentCategory.FOREX, page, outputsize)

    async def fetch_commodities(self, *, page: int, outputsize: int) -> InstrumentCataloguePage:
        return await self._page(InstrumentCategory.COMMODITY, page, outputsize)

    async def fetch_cryptocurrencies(self, *, page: int, outputsize: int) -> InstrumentCataloguePage:
        return await self._page(InstrumentCategory.CRYPTO, page, outputsize)

    async def _page(
        self, category: InstrumentCategory, page: int, outputsize: int
    ) -> InstrumentCataloguePage:
        self.calls.append((category, page))
        if self.delay:
            await asyncio.sleep(0.02)
        if self.fail:
            raise RuntimeError("provider unavailable")
        records = self.pages[category].get(page, ())
        return InstrumentCataloguePage(
            source_id="twelve_data",
            category=category,
            page=page,
            outputsize=outputsize,
            provider_count=sum(len(value) for value in self.pages[category].values()),
            instruments=records,
        )


def _service(tmp_path: Path, client: StubCatalogueClient) -> InstrumentCatalogueService:
    return InstrumentCatalogueService(
        store=InstrumentCatalogueStore(tmp_path / "catalogue.sqlite3"),
        client=client,  # type: ignore[arg-type]
    )


def _create_legacy_database(
    path: Path,
    entry: InstrumentCatalogueEntry | tuple[InstrumentCatalogueEntry, ...],
    *,
    versioned: bool = False,
) -> None:
    entries = (entry,) if isinstance(entry, InstrumentCatalogueEntry) else entry
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE catalogue_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE catalogue_instruments (
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
            );
            CREATE INDEX catalogue_instruments_lookup
                ON catalogue_instruments (category, forex_group, provider_symbol);
            """
        )
        connection.executemany(
            """INSERT INTO catalogue_instruments (
                instrument_id, source_id, category, provider_symbol, display_symbol,
                display_name, base_currency, quote_currency, forex_group,
                commodity_category, description, available_exchanges_json, quote_access
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    item.instrument_id,
                    item.source_id,
                    item.category.value,
                    item.provider_symbol,
                    item.display_symbol,
                    item.display_name,
                    item.base_currency,
                    item.quote_currency,
                    item.forex_group,
                    item.commodity_category,
                    item.description,
                    json.dumps(item.available_exchanges),
                    item.quote_access,
                )
                for item in entries
            ],
        )
        connection.execute(
            "INSERT INTO catalogue_metadata(key, value) VALUES (?, ?)",
            ("refreshed_at", datetime(2026, 1, 1, tzinfo=UTC).isoformat()),
        )
        if versioned:
            connection.executemany(
                "INSERT INTO catalogue_metadata(key, value) VALUES (?, ?)",
                (("schema_version", "1"), ("migration_state", "READY")),
            )


def test_fresh_database_initializes_versioned_schema(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.sqlite3"

    InstrumentCatalogueStore(path)

    with sqlite3.connect(path) as connection:
        metadata = dict(
            connection.execute(
                "SELECT key, value FROM catalogue_metadata "
                "WHERE key IN ('schema_version', 'migration_state')"
            )
        )
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    assert metadata == {"schema_version": "2", "migration_state": "READY"}
    assert objects == {
        "catalogue_metadata",
        "catalogue_instruments",
        "catalogue_provider_mappings",
        "catalogue_aliases",
        "catalogue_provenance",
        "catalogue_instruments_lookup",
        "catalogue_aliases_lookup",
        "catalogue_provenance_source_lookup",
    }


def test_fresh_database_commit_failure_rolls_back_entire_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-bootstrap.sqlite3"

    def fail_commit(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected bootstrap commit failure")

    monkeypatch.setattr(InstrumentCatalogueStore, "_commit", staticmethod(fail_commit))

    with pytest.raises(
        sqlite3.OperationalError, match="injected bootstrap commit failure"
    ):
        InstrumentCatalogueStore(path)

    with sqlite3.connect(path) as connection:
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert objects == []


@pytest.mark.parametrize("versioned", [False, True])
def test_exact_v1_schema_is_migrated_without_record_loss(
    tmp_path: Path, versioned: bool,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    expected = _entry(InstrumentCategory.FOREX, "EUR/USD", forex_group="Major")
    _create_legacy_database(path, expected, versioned=versioned)

    store = InstrumentCatalogueStore(path)

    migrated = store.get(expected.instrument_id)
    assert migrated is not None
    assert migrated.instrument_id == expected.instrument_id
    assert migrated.provider_symbol == expected.provider_symbol
    assert migrated.display_symbol == expected.display_symbol
    assert migrated.forex_group == expected.forex_group
    assert migrated.exchange_id.startswith("LEGACY_UNRESOLVED:")
    assert migrated.exchange_resolution is ExchangeResolution.LEGACY_UNRESOLVED
    assert migrated.provider_mappings[0].provider_instrument_id == expected.instrument_id
    assert migrated.source_provenance[0].source_record_id == expected.instrument_id
    assert migrated.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert migrated.updated_at == datetime(2026, 1, 1, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM catalogue_metadata"))
        count = connection.execute("SELECT COUNT(*) FROM catalogue_instruments").fetchone()[0]
        mapping_count = connection.execute(
            "SELECT COUNT(*) FROM catalogue_provider_mappings"
        ).fetchone()[0]
        provenance_count = connection.execute(
            "SELECT COUNT(*) FROM catalogue_provenance"
        ).fetchone()[0]
    assert metadata["schema_version"] == "2"
    assert metadata["migration_state"] == "READY"
    assert metadata["refreshed_at"] == datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    assert count == 1
    assert mapping_count == 1
    assert provenance_count == 1


def test_invalid_legacy_schema_fails_closed_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE catalogue_metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE catalogue_instruments (
                instrument_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL
            );
            """
        )

    with pytest.raises(InstrumentCatalogueSchemaError, match="schema is invalid"):
        InstrumentCatalogueStore(path)

    with sqlite3.connect(path) as connection:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(catalogue_instruments)")
        ]
        version = connection.execute(
            "SELECT value FROM catalogue_metadata WHERE key = 'schema_version'"
        ).fetchone()
    assert columns == ["instrument_id", "source_id"]
    assert version is None


def test_legacy_adoption_commit_failure_rolls_back_metadata_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    expected = _entry(InstrumentCategory.CRYPTO, "BTC/USD")
    _create_legacy_database(path, expected)

    def fail_commit(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected commit failure")

    monkeypatch.setattr(InstrumentCatalogueStore, "_commit", staticmethod(fail_commit))

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        InstrumentCatalogueStore(path)

    with sqlite3.connect(path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM catalogue_metadata"))
        rows = connection.execute(
            "SELECT instrument_id FROM catalogue_instruments"
        ).fetchall()
    assert "schema_version" not in metadata
    assert "migration_state" not in metadata
    assert rows == [(expected.instrument_id,)]


def test_v1_to_v2_migration_failure_rolls_back_schema_metadata_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-v2-migration.sqlite3"
    expected = _entry(InstrumentCategory.COMMODITY, "XAU/USD")
    _create_legacy_database(path, expected, versioned=True)
    apply_migration = InstrumentCatalogueStore._apply_v2_migration

    def fail_after_migration(
        self: InstrumentCatalogueStore, connection: sqlite3.Connection
    ) -> None:
        apply_migration(self, connection)
        raise sqlite3.OperationalError("injected v2 migration failure")

    monkeypatch.setattr(
        InstrumentCatalogueStore, "_apply_v2_migration", fail_after_migration
    )

    with pytest.raises(sqlite3.OperationalError, match="injected v2 migration failure"):
        InstrumentCatalogueStore(path)

    with sqlite3.connect(path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM catalogue_metadata"))
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(catalogue_instruments)")
        ]
        rows = connection.execute(
            "SELECT instrument_id, provider_symbol FROM catalogue_instruments"
        ).fetchall()
    assert metadata["schema_version"] == "1"
    assert metadata["migration_state"] == "READY"
    assert "canonical_instrument_id" not in columns
    assert rows == [(expected.instrument_id, expected.provider_symbol)]


def test_canonical_model_and_store_support_all_asset_classes(tmp_path: Path) -> None:
    assert set(InstrumentCategory) == {
        InstrumentCategory.FOREX,
        InstrumentCategory.COMMODITY,
        InstrumentCategory.CRYPTO,
        InstrumentCategory.STOCK,
        InstrumentCategory.ETF,
        InstrumentCategory.FUTURE,
        InstrumentCategory.INDEX,
        InstrumentCategory.BOND,
    }
    entries = tuple(
        _entry(category, f"SYM{index}", exchange_id=f"XVENUE{index}")
        for index, category in enumerate(InstrumentCategory, start=1)
    )
    store = InstrumentCatalogueStore(tmp_path / "all-asset-classes.sqlite3")

    store.replace_snapshot(entries, refreshed_at=NOW)

    assert store.status().instrument_count == 8
    for expected in entries:
        stored = store.get(expected.instrument_id)
        assert stored == expected


def test_same_ticker_on_different_exchanges_has_distinct_identity_and_mapping(
    tmp_path: Path,
) -> None:
    first = _entry(InstrumentCategory.STOCK, "ABC", exchange_id="XNYS")
    second = _entry(InstrumentCategory.STOCK, "ABC", exchange_id="XNAS")
    store = InstrumentCatalogueStore(tmp_path / "exchange-aware.sqlite3")

    store.replace_snapshot((first, second), refreshed_at=NOW)

    total, stored = store.search(
        query="ABC", category=InstrumentCategory.STOCK, forex_group=None, page=1, limit=10
    )
    assert total == 2
    assert {item.exchange_id for item in stored} == {"XNYS", "XNAS"}
    assert len({item.canonical_instrument_id for item in stored}) == 2
    assert {
        item.provider_mappings[0].provider_exchange_id for item in stored
    } == {"XNYS", "XNAS"}
    assert all(item.quote_access == "unknown" for item in stored)
    assert all(item.historical_data_access == "unknown" for item in stored)


def test_dated_futures_with_same_root_and_exchange_are_distinct(tmp_path: Path) -> None:
    december = _entry(
        InstrumentCategory.FUTURE,
        "ES",
        exchange_id="XCME",
        contract_kind=InstrumentContractKind.DATED,
        contract_expiry=date(2026, 12, 18),
    )
    march = _entry(
        InstrumentCategory.FUTURE,
        "ES",
        exchange_id="XCME",
        contract_kind=InstrumentContractKind.DATED,
        contract_expiry=date(2027, 3, 19),
    )
    store = InstrumentCatalogueStore(tmp_path / "dated-futures.sqlite3")

    store.replace_snapshot((december, march), refreshed_at=NOW)

    assert december.instrument_id != march.instrument_id
    assert december.canonical_instrument_id != march.canonical_instrument_id
    assert store.status().instrument_count == 2
    assert store.get(december.instrument_id) == december
    assert store.get(march.instrument_id) == march


def test_continuous_and_dated_futures_are_distinct(tmp_path: Path) -> None:
    continuous = _entry(
        InstrumentCategory.FUTURE,
        "CL",
        exchange_id="XNYM",
        contract_kind=InstrumentContractKind.CONTINUOUS,
        continuous_contract_rank=1,
    )
    dated = _entry(
        InstrumentCategory.FUTURE,
        "CL",
        exchange_id="XNYM",
        contract_kind=InstrumentContractKind.DATED,
        contract_expiry=date(2027, 1, 20),
    )
    store = InstrumentCatalogueStore(tmp_path / "continuous-futures.sqlite3")

    store.replace_snapshot((continuous, dated), refreshed_at=NOW)

    assert continuous.canonical_instrument_id != dated.canonical_instrument_id
    assert continuous.provider_mappings[0].provider_contract_id != (
        dated.provider_mappings[0].provider_contract_id
    )
    assert store.status().instrument_count == 2


def test_v1_duplicate_symbols_from_different_sources_migrate_without_collision(
    tmp_path: Path,
) -> None:
    first = _entry(InstrumentCategory.CRYPTO, "BTC/USD", source_id="source_one")
    second = _entry(InstrumentCategory.CRYPTO, "BTC/USD", source_id="source_two")
    path = tmp_path / "duplicate-legacy-symbols.sqlite3"
    _create_legacy_database(path, (first, second), versioned=True)

    store = InstrumentCatalogueStore(path)

    migrated = (store.get(first.instrument_id), store.get(second.instrument_id))
    assert all(item is not None for item in migrated)
    assert len({item.canonical_instrument_id for item in migrated if item}) == 2
    assert all(
        item.exchange_resolution is ExchangeResolution.LEGACY_UNRESOLVED
        for item in migrated
        if item
    )
    assert len({item.exchange_id for item in migrated if item}) == 2


def test_v1_multiple_records_preserve_nullable_fields_and_exchange_lists(
    tmp_path: Path,
) -> None:
    first = _entry(
        InstrumentCategory.CRYPTO,
        "ETH/USD",
        exchanges=("Binance", "Coinbase"),
    ).model_copy(
        update={
            "display_name": None,
            "base_currency": None,
            "quote_currency": None,
            "description": None,
        }
    )
    second = _entry(
        InstrumentCategory.FOREX,
        "EUR/USD",
        forex_group="Major",
        exchanges=("Global FX",),
    ).model_copy(
        update={
            "base_currency": "EUR",
            "quote_currency": "USD",
            "description": "Euro versus US dollar",
        }
    )
    path = tmp_path / "multiple-legacy-records.sqlite3"
    _create_legacy_database(path, (first, second))

    store = InstrumentCatalogueStore(path)

    migrated_first = store.get(first.instrument_id)
    migrated_second = store.get(second.instrument_id)
    assert migrated_first is not None
    assert migrated_second is not None
    assert migrated_first.display_name is None
    assert migrated_first.base_currency is None
    assert migrated_first.available_exchanges == ("Binance", "Coinbase")
    assert migrated_second.base_currency == "EUR"
    assert migrated_second.quote_currency == "USD"
    assert migrated_second.forex_group == "Major"
    assert migrated_second.description == "Euro versus US dollar"
    assert migrated_second.available_exchanges == ("Global FX",)


def test_existing_v2_database_reopens_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "existing-v2.sqlite3"
    expected = _entry(InstrumentCategory.ETF, "SPY", exchange_id="ARCX")
    first = InstrumentCatalogueStore(path)
    first.replace_snapshot((expected,), refreshed_at=NOW)

    reopened = InstrumentCatalogueStore(path)

    assert reopened.get(expected.instrument_id) == expected
    assert reopened.status().instrument_count == 1


def test_cache_refresh_search_filters_pagination_and_lookup(tmp_path: Path) -> None:
    client = StubCatalogueClient()
    service = _service(tmp_path, client)

    all_items = _run(service.list_instruments(limit=2, page=1))
    forex = _run(service.list_instruments(category=InstrumentCategory.FOREX, limit=10))
    major = _run(service.list_instruments(forex_group="Major", limit=10))
    searched = _run(service.list_instruments(query="btc", limit=10))
    alias_search = _run(service.list_instruments(query="EURUSD", limit=10))
    lookup = _run(service.get_instrument("twelve_data:commodity:XAU/USD"))

    assert all_items.count == 4
    assert len(all_items.instruments) == 2
    assert all_items.is_stale is False
    assert forex.count == 2
    assert [item.provider_symbol for item in major.instruments] == ["EUR/USD"]
    assert [item.provider_symbol for item in searched.instruments] == ["BTC/USD"]
    assert [item.provider_symbol for item in alias_search.instruments] == ["EUR/USD"]
    assert lookup is not None
    assert lookup.instrument.provider_symbol == "XAU/USD"
    assert lookup.instrument.quote_access == "unknown"
    assert sorted(client.calls, key=lambda value: (value[0].value, value[1])) == sorted([
        (InstrumentCategory.FOREX, 1),
        (InstrumentCategory.COMMODITY, 1),
        (InstrumentCategory.CRYPTO, 1),
        (InstrumentCategory.FOREX, 2),
    ], key=lambda value: (value[0].value, value[1]))


def test_concurrent_initial_reads_share_a_single_refresh(tmp_path: Path) -> None:
    client = StubCatalogueClient()
    client.delay = True
    service = _service(tmp_path, client)

    async def concurrent_reads() -> tuple[Any, Any]:
        return await asyncio.gather(
            service.list_instruments(limit=10), service.list_instruments(limit=10)
        )

    first, second = _run(concurrent_reads())

    assert first.count == second.count == 4
    assert len(client.calls) == 4


def test_failed_refresh_preserves_and_marks_the_last_valid_cache_stale(tmp_path: Path) -> None:
    client = StubCatalogueClient()
    service = _service(tmp_path, client)
    _run(service.list_instruments(limit=10))
    client.fail = True

    status = _run(service.refresh(force=True))
    response = _run(service.list_instruments(limit=10))

    assert status.available is True
    assert status.is_stale is True
    assert status.last_refresh_error == "catalogue_refresh_failed"
    assert response.count == 4
    assert response.is_stale is True


def test_incomplete_refresh_never_replaces_the_valid_cache(tmp_path: Path) -> None:
    client = StubCatalogueClient()
    service = _service(tmp_path, client)
    _run(service.list_instruments(limit=10))
    client.pages[InstrumentCategory.COMMODITY] = {1: ()}

    status = _run(service.refresh(force=True))
    response = _run(service.list_instruments(limit=10))

    assert status.instrument_count == 4
    assert status.last_refresh_error == "catalogue_refresh_failed"
    assert response.count == 4


def test_status_and_routes_are_read_only_and_report_cached_results(tmp_path: Path) -> None:
    service = _service(tmp_path, StubCatalogueClient())
    app.dependency_overrides[get_instrument_catalogue_service] = lambda: service

    async def request(path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    try:
        listed = _run(request("/api/instruments?q=eur&category=forex"))
        status = _run(request("/api/instruments/status"))
        detail = _run(request("/api/instruments/twelve_data:forex:EUR/USD"))
    finally:
        app.dependency_overrides.pop(get_instrument_catalogue_service, None)

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert status.status_code == 200
    assert status.json()["available"] is True
    assert detail.status_code == 200
    assert detail.json()["instrument"]["quote_access"] == "unknown"
