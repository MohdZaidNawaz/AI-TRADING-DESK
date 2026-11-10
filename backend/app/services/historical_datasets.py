"""Bounded ingestion and immutable persistence for historical research datasets."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Callable, Protocol
from uuid import uuid4

from app.connectors.twelve_data import (
    TWELVE_DATA_HISTORICAL_REQUEST_TIMEZONE,
    TWELVE_DATA_MAX_OUTPUT_SIZE,
    TwelveDataClient,
    TwelveDataConfigurationError,
    TwelveDataHistoricalConnector,
    TwelveDataPayloadError,
    TwelveDataProviderError,
    twelve_data_historical_connector,
)
from app.evidence.models import MarketSeriesEvidence
from app.models.historical_datasets import (
    DatasetCompleteness,
    DatasetIngestionStatus,
    HistoricalDatasetIngestionRequest,
    HistoricalDatasetIngestionResponse,
    HistoricalDatasetListResponse,
    HistoricalDatasetMetadata,
    HistoricalDatasetRecord,
)
from app.models.market_series import (
    MarketBarState,
    MarketSeriesKey,
    NormalizedMarketBar,
    canonical_market_symbol,
    timeframe_seconds,
)
from app.models.research_requests import InstrumentResolutionStatus
from app.services.research_requests import ResearchRequestService


DATASET_SCHEMA_VERSION = 1
DATASET_VERSION = 1
DEFAULT_DATASET_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "historical-datasets"
_DATASET_FILE = re.compile(r"^dataset-(historical-[0-9a-f]{32})\.json$")


class HistoricalDatasetError(RuntimeError):
    pass


class HistoricalDatasetNotFoundError(HistoricalDatasetError):
    pass


class HistoricalDatasetCorruptError(HistoricalDatasetError):
    pass


class HistoricalDatasetCapacityError(HistoricalDatasetError):
    pass


class HistoricalClient(Protocol):
    async def fetch_time_series(
        self,
        symbol: str,
        interval: str,
        *,
        outputsize: int,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        exchange: str | None = None,
    ): ...


class HistoricalDatasetStore:
    """Immutable JSON dataset store with atomic publication and strict revalidation."""

    def __init__(self, root: Path = DEFAULT_DATASET_ROOT, *, maximum_datasets: int = 100) -> None:
        if maximum_datasets < 1 or maximum_datasets > 1_000:
            raise ValueError("maximum_datasets must be between 1 and 1000")
        self._root = Path(root)
        self._maximum_datasets = maximum_datasets
        self._lock = RLock()

    @property
    def root(self) -> Path:
        return self._root

    def save(self, record: HistoricalDatasetRecord) -> HistoricalDatasetMetadata:
        validated = _validate_record(record)
        target = self._path(validated.metadata.dataset_id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing = self.load(validated.metadata.dataset_id)
                if existing != validated:
                    raise HistoricalDatasetCorruptError("dataset identity collision")
                return existing.metadata
            if len(tuple(self._dataset_paths())) >= self._maximum_datasets:
                raise HistoricalDatasetCapacityError("historical dataset retention limit reached")
            temporary = self._root / f".{target.name}.{uuid4().hex}.tmp"
            payload = validated.model_dump_json(indent=2, exclude_computed_fields=True)
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                    existing = self.load(validated.metadata.dataset_id)
                    if existing != validated:
                        raise HistoricalDatasetCorruptError("dataset identity collision")
                    return existing.metadata
                try:
                    temporary.unlink()
                except OSError:
                    pass
            except Exception:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        return validated.metadata

    def load(self, dataset_id: str) -> HistoricalDatasetRecord:
        path = self._path(dataset_id)
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise HistoricalDatasetNotFoundError("historical dataset was not found") from None
            except OSError as exc:
                raise HistoricalDatasetCorruptError("historical dataset cannot be read") from exc
        try:
            return _validate_record(HistoricalDatasetRecord.model_validate_json(raw))
        except (ValueError, TypeError) as exc:
            raise HistoricalDatasetCorruptError("historical dataset failed integrity validation") from exc

    def list(self) -> HistoricalDatasetListResponse:
        datasets: list[HistoricalDatasetMetadata] = []
        corrupt = 0
        with self._lock:
            paths = tuple(self._dataset_paths())
        for path in paths:
            match = _DATASET_FILE.fullmatch(path.name)
            if match is None:
                continue
            try:
                datasets.append(self.load(match.group(1)).metadata)
            except HistoricalDatasetError:
                corrupt += 1
        datasets.sort(key=lambda item: (item.created_at, item.dataset_id), reverse=True)
        return HistoricalDatasetListResponse(datasets=tuple(datasets), corrupt_dataset_count=corrupt)

    def series(self, dataset_id: str) -> tuple[HistoricalDatasetMetadata, MarketSeriesEvidence]:
        record = self.load(dataset_id)
        first = record.bars[0]
        key = MarketSeriesKey(
            source_id=first.source.source_id,
            symbol=first.symbol,
            venue=first.venue,
            timeframe=first.timeframe,
        )
        return record.metadata, MarketSeriesEvidence.from_bars(key, record.bars)

    def _dataset_paths(self):
        if not self._root.is_dir():
            return iter(())
        return (path for path in self._root.iterdir() if path.is_file() and _DATASET_FILE.fullmatch(path.name))

    def _path(self, dataset_id: str) -> Path:
        if not isinstance(dataset_id, str) or not re.fullmatch(r"historical-[0-9a-f]{32}", dataset_id):
            raise HistoricalDatasetNotFoundError("historical dataset identity is invalid")
        return self._root / f"dataset-{dataset_id}.json"


class HistoricalDatasetService:
    """Explicit, sequential Twelve Data ingestion; never invoked by backtesting."""

    def __init__(
        self,
        *,
        store: HistoricalDatasetStore,
        resolver: ResearchRequestService,
        client: HistoricalClient | None,
        connector: TwelveDataHistoricalConnector = twelve_data_historical_connector,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._client = client
        self._connector = connector
        self._now = now or (lambda: datetime.now(UTC))
        self._ingestion_lock = asyncio.Lock()
        self._rate_limited = False

    async def ingest(self, request: HistoricalDatasetIngestionRequest) -> HistoricalDatasetIngestionResponse:
        async with self._ingestion_lock:
            return await self._ingest_locked(request)

    async def _ingest_locked(self, request: HistoricalDatasetIngestionRequest) -> HistoricalDatasetIngestionResponse:
        resolution = self._resolver.resolve_instrument(request.symbol)
        if resolution.status is not InstrumentResolutionStatus.RESOLVED or resolution.canonical_symbol is None:
            return _failure(DatasetIngestionStatus.UNAVAILABLE, "INSTRUMENT_IDENTITY_NOT_RESOLVED")
        if resolution.provider_source_id not in (None, "twelve_data"):
            return _failure(DatasetIngestionStatus.UNAVAILABLE, "TWELVE_DATA_MAPPING_UNAVAILABLE")
        provider_symbol = resolution.provider_symbol or resolution.canonical_symbol
        if self._rate_limited:
            return _failure(DatasetIngestionStatus.RATE_LIMITED, "TWELVE_DATA_RATE_LIMITED", category="rate_limit")
        if self._client is None:
            return _failure(DatasetIngestionStatus.NOT_CONFIGURED, "TWELVE_DATA_NOT_CONFIGURED", category="configuration")

        next_start = request.start_at
        duration = timedelta(seconds=timeframe_seconds(request.timeframe))
        by_open: dict[datetime, NormalizedMarketBar] = {}
        warnings: list[str] = []
        request_count = 0
        received_count = 0
        provider_failure: tuple[DatasetIngestionStatus, str, str] | None = None
        last_batch = None
        provider_identity_verified = resolution.provider_exchange_id is None

        while request_count < request.max_requests and len(by_open) < request.max_bars and next_start < request.end_at:
            outputsize = min(TWELVE_DATA_MAX_OUTPUT_SIZE, request.max_bars - len(by_open))
            request_count += 1
            retrieved_at = _utc(self._now())
            try:
                payload = await self._client.fetch_time_series(
                    provider_symbol,
                    request.timeframe,
                    outputsize=outputsize,
                    start_date=next_start,
                    end_date=request.end_at,
                    exchange=resolution.provider_exchange_id,
                )
                batch = self._connector.normalize(
                    payload,
                    expected_symbol=provider_symbol,
                    timeframe=request.timeframe,
                    requested_count=outputsize,
                    retrieved_at=retrieved_at,
                    requested_timezone=TWELVE_DATA_HISTORICAL_REQUEST_TIMEZONE,
                )
            except TwelveDataProviderError as exc:
                status = _provider_status(exc.category)
                if exc.category == "rate_limit":
                    self._rate_limited = True
                provider_failure = (status, _provider_reason(exc.category), exc.category)
                break
            except TwelveDataPayloadError as exc:
                provider_failure = (
                    DatasetIngestionStatus.INVALID_RESPONSE,
                    "TWELVE_DATA_HISTORICAL_RESPONSE_INVALID",
                    exc.category,
                )
                break

            if (
                resolution.provider_exchange_id is not None
                and batch.venue is not None
                and batch.venue.casefold() != resolution.provider_exchange_id.casefold()
            ):
                return _failure(
                    DatasetIngestionStatus.INVALID_RESPONSE,
                    "HISTORICAL_PROVIDER_EXCHANGE_MISMATCH",
                    category="identity_mismatch",
                    request_count=request_count,
                    received_count=received_count + len(batch.bars),
                )
            if last_batch is not None and batch.venue != last_batch.venue:
                return _failure(
                    DatasetIngestionStatus.INVALID_RESPONSE,
                    "HISTORICAL_PROVIDER_VENUE_CHANGED",
                    category="identity_mismatch",
                    request_count=request_count,
                    received_count=received_count + len(batch.bars),
                )
            if resolution.provider_exchange_id is not None and batch.venue is not None:
                provider_identity_verified = True
            last_batch = batch
            received_count += len(batch.bars)
            warnings.extend(batch.warning_codes)
            page_latest: datetime | None = None
            for bar in batch.bars:
                if bar.opened_at < request.start_at or bar.opened_at >= request.end_at:
                    continue
                existing = by_open.get(bar.opened_at)
                if existing is not None and not _equivalent_overlap(existing, bar):
                    return _failure(
                        DatasetIngestionStatus.INVALID_RESPONSE,
                        "HISTORICAL_OVERLAP_CONFLICT",
                        category="overlap_conflict",
                        request_count=request_count,
                        received_count=received_count,
                    )
                by_open[bar.opened_at] = bar
                page_latest = max(page_latest, bar.opened_at) if page_latest else bar.opened_at
            if not batch.bars or page_latest is None:
                break
            candidate = page_latest + duration
            if candidate <= next_start:
                warnings.append("HISTORICAL_PROVIDER_MADE_NO_PROGRESS")
                break
            next_start = candidate
            if next_start >= request.end_at or len(batch.bars) < outputsize:
                break

        bars = tuple(by_open[key] for key in sorted(by_open))
        if not bars:
            if provider_failure is not None:
                return _failure(
                    provider_failure[0], provider_failure[1], category=provider_failure[2],
                    request_count=request_count, received_count=received_count,
                )
            return _failure(
                DatasetIngestionStatus.UNAVAILABLE,
                "HISTORICAL_DATASET_EMPTY",
                category="empty_response",
                request_count=request_count,
                received_count=received_count,
            )

        observed_gap_count = _observed_gap_count(bars, timeframe_seconds(request.timeframe))
        if observed_gap_count:
            warnings.extend(("HISTORICAL_DISCONTINUITIES_OBSERVED", "TRADING_CALENDAR_UNAVAILABLE"))
        provisional_count = sum(bar.state is MarketBarState.PROVISIONAL for bar in bars)
        coverage_start = bars[0].opened_at
        coverage_end = bars[-1].closed_at
        complete = (
            provider_failure is None
            and coverage_start <= request.start_at + duration
            and coverage_end >= request.end_at
            and provisional_count == 0
            and provider_identity_verified
            and len(bars) < request.max_bars
        )
        if request_count >= request.max_requests and coverage_end < request.end_at:
            warnings.append("HISTORICAL_REQUEST_LIMIT_REACHED")
        if len(bars) >= request.max_bars and coverage_end < request.end_at:
            warnings.append("HISTORICAL_BAR_LIMIT_REACHED")
        if coverage_end < request.end_at:
            warnings.append("HISTORICAL_COVERAGE_PARTIAL")
        if provider_failure is not None:
            warnings.append(provider_failure[1])
        if not provider_identity_verified:
            warnings.append("HISTORICAL_PROVIDER_EXCHANGE_UNVERIFIED")
        warnings.append("ADJUSTMENT_SEMANTICS_NOT_SUPPLIED_BY_PROVIDER")

        record = _build_record(
            request=request,
            resolution=resolution,
            provider_symbol=provider_symbol,
            bars=bars,
            request_count=request_count,
            last_batch=last_batch,
            provider_identity_verified=provider_identity_verified,
            observed_gap_count=observed_gap_count,
            completeness=DatasetCompleteness.COMPLETE if complete else DatasetCompleteness.PARTIAL,
            warning_codes=tuple(dict.fromkeys(warnings)),
            created_at=_utc(self._now()),
        )
        try:
            metadata = self._store.save(record)
        except (OSError, HistoricalDatasetError):
            return _failure(
                DatasetIngestionStatus.STORAGE_ERROR,
                "HISTORICAL_DATASET_STORAGE_FAILED",
                category="storage",
                request_count=request_count,
                received_count=received_count,
                validated_count=len(bars),
            )
        return HistoricalDatasetIngestionResponse(
            status=(DatasetIngestionStatus.AVAILABLE if complete else DatasetIngestionStatus.PARTIAL),
            dataset=metadata,
            provider_request_count=request_count,
            received_bar_count=received_count,
            validated_bar_count=len(bars),
            reason_codes=("HISTORICAL_DATASET_STORED", *metadata.warning_codes),
            error_category=provider_failure[2] if provider_failure else None,
        )


def _build_record(*, request, resolution, provider_symbol, bars, request_count, last_batch, provider_identity_verified, observed_gap_count, completeness, warning_codes, created_at):
    core = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "provider_id": "twelve_data",
        "provider_symbol": provider_symbol,
        "canonical_symbol": resolution.canonical_symbol,
        "canonical_instrument_id": resolution.canonical_instrument_id,
        "requested_provider_exchange_id": resolution.provider_exchange_id,
        "timeframe": request.timeframe,
        "venue": last_batch.venue if last_batch else bars[0].venue,
        "exchange_metadata_available": bool(last_batch and last_batch.exchange_metadata_available),
        "provider_identity_verified": provider_identity_verified,
        "adjustment_semantics": "provider_unspecified",
        "requested_start_at": request.start_at,
        "requested_end_at": request.end_at,
        "coverage_start": bars[0].opened_at,
        "coverage_end": bars[-1].closed_at,
        "retrieved_at": max(bar.received_at for bar in bars),
        "validated_bar_count": len(bars),
        "completed_bar_count": sum(bar.state is MarketBarState.COMPLETED for bar in bars),
        "provisional_bar_count": sum(bar.state is MarketBarState.PROVISIONAL for bar in bars),
        "observed_gap_count": observed_gap_count,
        "missing_interval_count": None,
        "provider_request_count": request_count,
        "completeness": completeness,
        "warning_codes": warning_codes,
        "created_at": created_at,
    }
    fingerprint = _content_fingerprint(core, bars)
    metadata = HistoricalDatasetMetadata(
        dataset_id=f"historical-{fingerprint[:32]}",
        content_fingerprint=fingerprint,
        **core,
    )
    return HistoricalDatasetRecord(metadata=metadata, bars=bars)


def _content_fingerprint(core: dict, bars: tuple[NormalizedMarketBar, ...]) -> str:
    serialized = {
        "metadata": HistoricalDatasetMetadata(
            dataset_id="historical-" + "0" * 32,
            content_fingerprint="0" * 64,
            **core,
        ).model_dump(mode="json", exclude={"dataset_id", "content_fingerprint"}),
        "bars": [bar.model_dump(mode="json") for bar in bars],
    }
    return sha256(json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_record(record: HistoricalDatasetRecord) -> HistoricalDatasetRecord:
    bars = record.bars
    metadata = record.metadata
    if tuple(sorted(bars, key=lambda item: item.opened_at)) != bars:
        raise HistoricalDatasetCorruptError("historical dataset is not ordered")
    if len({bar.opened_at for bar in bars}) != len(bars):
        raise HistoricalDatasetCorruptError("historical dataset contains duplicate timestamps")
    if any(bar.timeframe != metadata.timeframe for bar in bars):
        raise HistoricalDatasetCorruptError("historical dataset timeframe mismatch")
    if any(
        bar.symbol != bars[0].symbol
        or bar.venue != bars[0].venue
        or bar.source != bars[0].source
        for bar in bars
    ):
        raise HistoricalDatasetCorruptError("historical dataset mixes identities")
    completed_count = sum(bar.state is MarketBarState.COMPLETED for bar in bars)
    provisional_count = len(bars) - completed_count
    if (
        metadata.provider_id != bars[0].source.source_id
        or canonical_market_symbol(metadata.provider_symbol) != bars[0].symbol
        or canonical_market_symbol(metadata.canonical_symbol) != bars[0].symbol
        or metadata.venue != bars[0].venue
        or metadata.coverage_start != bars[0].opened_at
        or metadata.coverage_end != bars[-1].closed_at
        or metadata.retrieved_at != max(bar.received_at for bar in bars)
        or metadata.validated_bar_count != len(bars)
        or metadata.completed_bar_count != completed_count
        or metadata.provisional_bar_count != provisional_count
        or metadata.observed_gap_count
        != _observed_gap_count(bars, timeframe_seconds(metadata.timeframe))
        or any(
            bar.opened_at < metadata.requested_start_at
            or bar.opened_at >= metadata.requested_end_at
            for bar in bars
        )
        or metadata.created_at < metadata.retrieved_at
        or (metadata.exchange_metadata_available and metadata.venue is None)
        or (
            metadata.requested_provider_exchange_id is not None
            and metadata.provider_identity_verified
            and (
                metadata.venue is None
                or metadata.venue.casefold()
                != metadata.requested_provider_exchange_id.casefold()
            )
        )
    ):
        raise HistoricalDatasetCorruptError("historical dataset metadata is inconsistent")
    if metadata.completeness is DatasetCompleteness.COMPLETE and (
        provisional_count
        or not metadata.provider_identity_verified
        or metadata.coverage_start
        > metadata.requested_start_at + timedelta(seconds=timeframe_seconds(metadata.timeframe))
        or metadata.coverage_end < metadata.requested_end_at
    ):
        raise HistoricalDatasetCorruptError("complete dataset coverage is inconsistent")
    core = metadata.model_dump(exclude={"dataset_id", "content_fingerprint"})
    expected = _content_fingerprint(core, bars)
    if metadata.content_fingerprint != expected or metadata.dataset_id != f"historical-{expected[:32]}":
        raise HistoricalDatasetCorruptError("historical dataset fingerprint mismatch")
    return record


def _observed_gap_count(bars: tuple[NormalizedMarketBar, ...], seconds: int) -> int:
    return sum(
        (current.opened_at - previous.opened_at).total_seconds() > seconds
        for previous, current in zip(bars, bars[1:], strict=False)
    )


def _equivalent_overlap(left: NormalizedMarketBar, right: NormalizedMarketBar) -> bool:
    """Ignore retrieval time only; conflicting provider revisions fail closed."""
    excluded = {"received_at", "timeframe_seconds", "bar_id"}
    return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)


def _failure(status, reason, *, category=None, request_count=0, received_count=0, validated_count=0):
    return HistoricalDatasetIngestionResponse(
        status=status,
        provider_request_count=request_count,
        received_bar_count=received_count,
        validated_bar_count=validated_count,
        reason_codes=(reason,),
        error_category=category,
    )


def _provider_status(category: str) -> DatasetIngestionStatus:
    if category == "rate_limit":
        return DatasetIngestionStatus.RATE_LIMITED
    if category == "timeout":
        return DatasetIngestionStatus.TIMEOUT
    return DatasetIngestionStatus.UNAVAILABLE


def _provider_reason(category: str) -> str:
    return {
        "rate_limit": "TWELVE_DATA_RATE_LIMITED",
        "timeout": "TWELVE_DATA_TIMEOUT",
        "network": "TWELVE_DATA_NETWORK_UNAVAILABLE",
    }.get(category, "TWELVE_DATA_PROVIDER_UNAVAILABLE")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require a timezone")
    return value.astimezone(UTC)


@lru_cache
def get_historical_dataset_store() -> HistoricalDatasetStore:
    return HistoricalDatasetStore()


@lru_cache
def get_historical_dataset_service() -> HistoricalDatasetService:
    from app.catalogue.store import InstrumentCatalogueSchemaError, read_catalogue_snapshot
    from app.services.instrument_catalogue import DEFAULT_CATALOGUE_CACHE_PATH

    try:
        entries = read_catalogue_snapshot(DEFAULT_CATALOGUE_CACHE_PATH)
    except (InstrumentCatalogueSchemaError, OSError, ValueError):
        entries = ()
    try:
        client: HistoricalClient | None = TwelveDataClient.from_environment()
    except TwelveDataConfigurationError:
        client = None
    return HistoricalDatasetService(
        store=get_historical_dataset_store(),
        resolver=ResearchRequestService(catalogue_entries=entries),
        client=client,
    )
