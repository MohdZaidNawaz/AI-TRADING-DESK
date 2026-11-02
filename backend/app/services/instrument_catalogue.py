"""Read-only, persistent Twelve Data instrument catalogue service."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from app.catalogue.store import InstrumentCatalogueStore, StoredCatalogueStatus
from app.config import get_twelve_data_api_key
from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConfigurationError,
    TwelveDataError,
)
from app.models.instruments import (
    InstrumentCatalogueEntry,
    InstrumentCatalogueInstrumentResponse,
    InstrumentCataloguePage,
    InstrumentCatalogueSearchResponse,
    InstrumentCatalogueStatus,
    InstrumentCategory,
)

CATALOGUE_REFRESH_INTERVAL = timedelta(days=1)
REFRESH_FAILURE_RETRY_DELAY = timedelta(minutes=5)
CATALOGUE_PAGE_SIZE = 500
MAX_CATALOGUE_PAGES = 1_000
DEFAULT_CATALOGUE_CACHE_PATH = (
    Path(__file__).resolve().parents[2] / "runtime" / "instrument_catalogue.sqlite3"
)


class InstrumentCatalogueUnavailable(RuntimeError):
    """No valid cached catalogue can be served."""


class InstrumentCatalogueService:
    """Refreshes all directories atomically and serves only valid snapshots."""

    def __init__(
        self,
        *,
        store: InstrumentCatalogueStore,
        client: TwelveDataClient | None,
    ) -> None:
        self._store = store
        self._client = client
        self._refresh_lock = asyncio.Lock()

    async def list_instruments(
        self,
        *,
        query: str | None = None,
        category: InstrumentCategory | None = None,
        forex_group: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> InstrumentCatalogueSearchResponse:
        self._validate_pagination(page, limit)
        status = await self._ensure_catalogue()
        count, instruments = self._store.search(
            query=_normalize_query(query),
            category=category,
            forex_group=_normalize_optional_text(forex_group),
            page=page,
            limit=limit,
        )
        return InstrumentCatalogueSearchResponse(
            count=count,
            page=page,
            limit=limit,
            is_stale=self._is_stale(status),
            refreshed_at=status.refreshed_at,
            instruments=instruments,
        )

    async def get_instrument(
        self, instrument_id: str
    ) -> InstrumentCatalogueInstrumentResponse | None:
        status = await self._ensure_catalogue()
        instrument = self._store.get(instrument_id)
        if instrument is None:
            return None
        return InstrumentCatalogueInstrumentResponse(
            is_stale=self._is_stale(status),
            refreshed_at=status.refreshed_at,
            instrument=instrument,
        )

    async def get_status(self) -> InstrumentCatalogueStatus:
        try:
            status = await self._ensure_catalogue()
        except InstrumentCatalogueUnavailable:
            status = self._store.status()
        return self._public_status(status)

    async def refresh(self, *, force: bool = False) -> InstrumentCatalogueStatus:
        """Refresh all categories for internal scheduling/tests; no API route exposes it."""
        return self._public_status(await self._ensure_catalogue(force=force))

    async def _ensure_catalogue(
        self, *, force: bool = False
    ) -> StoredCatalogueStatus:
        current = self._store.status()
        now = datetime.now(UTC)
        if not force and self._is_fresh(current, now):
            return current
        if not force and self._recently_failed(current, now):
            return self._serve_or_raise(current)

        async with self._refresh_lock:
            current = self._store.status()
            now = datetime.now(UTC)
            if not force and self._is_fresh(current, now):
                return current
            if not force and self._recently_failed(current, now):
                return self._serve_or_raise(current)

            self._store.mark_refresh_attempt(now)
            if self._client is None:
                self._store.mark_refresh_failure(
                    "provider_not_configured",
                    category="not_configured",
                )
                return self._serve_or_raise(self._store.status())

            try:
                instruments = await self._fetch_complete_snapshot()
            except TwelveDataError as exc:
                self._store.mark_refresh_failure(
                    "provider_refresh_failed",
                    category=getattr(exc, "category", "provider_error"),
                    status_code=getattr(exc, "status_code", None),
                    provider_code=getattr(exc, "provider_code", None),
                )
                return self._serve_or_raise(self._store.status())
            except Exception:
                self._store.mark_refresh_failure(
                    "catalogue_refresh_failed",
                    category="unexpected_error",
                )
                return self._serve_or_raise(self._store.status())

            self._store.replace_snapshot(instruments, refreshed_at=now)
            return self._store.status()

    async def _fetch_complete_snapshot(self) -> tuple[InstrumentCatalogueEntry, ...]:
        if self._client is None:
            raise InstrumentCatalogueUnavailable("provider not configured")
        forex, commodities, crypto = await asyncio.gather(
            self._fetch_all_pages(InstrumentCategory.FOREX),
            self._fetch_all_pages(InstrumentCategory.COMMODITY),
            self._fetch_all_pages(InstrumentCategory.CRYPTO),
        )
        instruments = (*forex, *commodities, *crypto)
        if not instruments:
            raise InstrumentCatalogueUnavailable("empty catalogue")
        if len({entry.instrument_id for entry in instruments}) != len(instruments):
            raise InstrumentCatalogueUnavailable("duplicate catalogue identifiers")
        return instruments

    async def _fetch_all_pages(
        self, category: InstrumentCategory
    ) -> tuple[InstrumentCatalogueEntry, ...]:
        first = await self._fetch_page(category, page=1)
        if first.provider_count < 1 or not first.instruments:
            raise InstrumentCatalogueUnavailable("incomplete catalogue")
        records = list(first.instruments)
        expected_count = first.provider_count
        page = 2
        while len(records) < expected_count:
            if page > MAX_CATALOGUE_PAGES:
                raise InstrumentCatalogueUnavailable("catalogue pagination limit exceeded")
            next_page = await self._fetch_page(category, page=page)
            if next_page.provider_count != expected_count or not next_page.instruments:
                raise InstrumentCatalogueUnavailable("incomplete catalogue")
            records.extend(next_page.instruments)
            page += 1
        if len(records) != expected_count:
            raise InstrumentCatalogueUnavailable("incomplete catalogue")
        return tuple(records)

    async def _fetch_page(
        self, category: InstrumentCategory, *, page: int
    ) -> InstrumentCataloguePage:
        if self._client is None:
            raise InstrumentCatalogueUnavailable("provider not configured")
        if category is InstrumentCategory.FOREX:
            return await self._client.fetch_forex_pairs(
                page=page, outputsize=CATALOGUE_PAGE_SIZE
            )
        if category is InstrumentCategory.COMMODITY:
            return await self._client.fetch_commodities(
                page=page, outputsize=CATALOGUE_PAGE_SIZE
            )
        return await self._client.fetch_cryptocurrencies(
            page=page, outputsize=CATALOGUE_PAGE_SIZE
        )

    @staticmethod
    def _validate_pagination(page: int, limit: int) -> None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")

    @staticmethod
    def _is_fresh(status: StoredCatalogueStatus, now: datetime) -> bool:
        return (
            status.available
            and status.refreshed_at is not None
            and now - status.refreshed_at < CATALOGUE_REFRESH_INTERVAL
        )

    @staticmethod
    def _recently_failed(status: StoredCatalogueStatus, now: datetime) -> bool:
        return (
            status.last_refresh_error is not None
            and status.last_refresh_attempt_at is not None
            and now - status.last_refresh_attempt_at < REFRESH_FAILURE_RETRY_DELAY
        )

    def _is_stale(self, status: StoredCatalogueStatus) -> bool:
        return status.last_refresh_error is not None or not self._is_fresh(
            status, datetime.now(UTC)
        )

    @staticmethod
    def _serve_or_raise(status: StoredCatalogueStatus) -> StoredCatalogueStatus:
        if status.available:
            return status
        raise InstrumentCatalogueUnavailable("no valid catalogue cache")

    def _public_status(self, status: StoredCatalogueStatus) -> InstrumentCatalogueStatus:
        return InstrumentCatalogueStatus(
            available=status.available,
            is_stale=self._is_stale(status),
            instrument_count=status.instrument_count,
            refreshed_at=status.refreshed_at,
            last_refresh_attempt_at=status.last_refresh_attempt_at,
            last_refresh_error=status.last_refresh_error,
            last_refresh_error_category=status.last_refresh_error_category,
            last_refresh_error_status_code=status.last_refresh_error_status_code,
            last_refresh_error_provider_code=status.last_refresh_error_provider_code,
        )


@lru_cache(maxsize=1)
def get_instrument_catalogue_service() -> InstrumentCatalogueService:
    """Production dependency using process environment only, never an env file."""
    api_key = get_twelve_data_api_key(load_env_file=False)
    try:
        client = TwelveDataClient(api_key) if api_key else None
    except TwelveDataConfigurationError:
        client = None
    return InstrumentCatalogueService(
        store=InstrumentCatalogueStore(DEFAULT_CATALOGUE_CACHE_PATH),
        client=client,
    )


def _normalize_query(value: str | None) -> str | None:
    return _normalize_optional_text(value)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
