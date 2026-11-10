from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json

import pytest

from app.connectors.twelve_data import TwelveDataProviderError
from app.models.backtesting import BacktestRequest, BacktestStatus
from app.models.historical_datasets import (
    DatasetCompleteness,
    DatasetIngestionStatus,
    HistoricalDatasetIngestionRequest,
)
from app.services.backtesting import BacktestingService
from app.services.historical_datasets import (
    HistoricalDatasetCorruptError,
    HistoricalDatasetService,
    HistoricalDatasetStore,
)
from app.services.research_requests import ResearchRequestService


NOW = datetime(2026, 1, 10, tzinfo=UTC)
START = datetime(2025, 1, 1, tzinfo=UTC)


def _payload(*timestamps: datetime, symbol: str = "XAU/USD") -> dict:
    return {
        "meta": {"symbol": symbol, "interval": "15min", "exchange_timezone": "UTC"},
        "values": [
            {
                "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "10",
            }
            for timestamp in timestamps
        ],
    }


class _Client:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def fetch_time_series(self, symbol, interval, *, outputsize, start_date=None, end_date=None, exchange=None):
        self.calls.append((symbol, interval, outputsize, start_date, end_date, exchange))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _request(*, end: datetime = START + timedelta(minutes=30), max_requests: int = 3):
    return HistoricalDatasetIngestionRequest(
        symbol="XAU/USD",
        timeframe="15m",
        start_at=START,
        end_at=end,
        max_requests=max_requests,
        max_bars=10_000,
    )


def test_ingestion_paginates_with_supported_start_date_and_persists_exact_dataset(tmp_path):
    client = _Client([
        _payload(START, START + timedelta(minutes=15)),
    ])
    store = HistoricalDatasetStore(tmp_path / "datasets")
    service = HistoricalDatasetService(
        store=store,
        resolver=ResearchRequestService(),
        client=client,
        now=lambda: NOW,
    )

    result = asyncio.run(service.ingest(_request()))

    assert result.status is DatasetIngestionStatus.AVAILABLE
    assert result.dataset is not None
    assert result.dataset.completeness is DatasetCompleteness.COMPLETE
    assert result.dataset.completed_bar_count == 2
    assert result.dataset.missing_interval_count is None
    assert client.calls == [("XAU/USD", "15m", 5_000, START, START + timedelta(minutes=30), None)]
    loaded = store.load(result.dataset.dataset_id)
    assert loaded.metadata == result.dataset
    assert len(loaded.bars) == 2


def test_bounded_chunk_progress_and_overlap_deduplication(tmp_path):
    first = tuple(START + timedelta(minutes=15 * index) for index in range(5_000))
    boundary = first[-1] + timedelta(minutes=15)
    client = _Client([
        _payload(*first),
        _payload(first[-1], boundary, boundary + timedelta(minutes=15)),
    ])
    service = HistoricalDatasetService(
        store=HistoricalDatasetStore(tmp_path / "datasets"),
        resolver=ResearchRequestService(),
        client=client,
        now=lambda: NOW,
    )

    result = asyncio.run(service.ingest(
        _request(end=boundary + timedelta(minutes=30), max_requests=2)
    ))

    assert result.provider_request_count == 2
    assert result.validated_bar_count == 5_002
    assert client.calls[1][3] == boundary


def test_rate_limit_has_no_dataset_and_is_not_retried(tmp_path):
    client = _Client([TwelveDataProviderError(category="rate_limit", status_code=429)])
    store = HistoricalDatasetStore(tmp_path / "datasets")
    service = HistoricalDatasetService(
        store=store,
        resolver=ResearchRequestService(),
        client=client,
        now=lambda: NOW,
    )

    first = asyncio.run(service.ingest(_request()))
    second = asyncio.run(service.ingest(_request()))

    assert first.status is DatasetIngestionStatus.RATE_LIMITED
    assert second.status is DatasetIngestionStatus.RATE_LIMITED
    assert len(client.calls) == 1
    assert store.list().datasets == ()


def test_partial_provider_failure_preserves_only_validated_bars(tmp_path):
    page = tuple(START + timedelta(minutes=15 * index) for index in range(5_000))
    client = _Client([_payload(*page), TwelveDataProviderError(category="timeout")])
    service = HistoricalDatasetService(
        store=HistoricalDatasetStore(tmp_path / "datasets"),
        resolver=ResearchRequestService(),
        client=client,
        now=lambda: NOW,
    )

    result = asyncio.run(service.ingest(_request(end=page[-1] + timedelta(hours=1))))

    assert result.status is DatasetIngestionStatus.PARTIAL
    assert result.error_category == "timeout"
    assert result.dataset is not None
    assert result.dataset.validated_bar_count == 5_000
    assert "TWELVE_DATA_TIMEOUT" in result.dataset.warning_codes


def test_store_rejects_corrupt_content_and_does_not_silently_overwrite(tmp_path):
    # Build a record through the public service helper path, then corrupt only its persisted content.
    store = HistoricalDatasetStore(tmp_path / "datasets")
    service = HistoricalDatasetService(
        store=store,
        resolver=ResearchRequestService(),
        client=_Client([_payload(START, START + timedelta(minutes=15))]),
        now=lambda: NOW,
    )
    result = __import__("asyncio").run(service.ingest(_request()))
    assert result.dataset is not None
    path = store.root / f"dataset-{result.dataset.dataset_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bars"][0]["close"] = 100.5
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HistoricalDatasetCorruptError):
        store.load(result.dataset.dataset_id)
    assert store.list().corrupt_dataset_count == 1


def test_atomic_publish_failure_leaves_no_dataset_or_temporary_file(tmp_path, monkeypatch):
    source_store = HistoricalDatasetStore(tmp_path / "source")
    service = HistoricalDatasetService(
        store=source_store,
        resolver=ResearchRequestService(),
        client=_Client([_payload(START, START + timedelta(minutes=15))]),
        now=lambda: NOW,
    )
    result = asyncio.run(service.ingest(_request()))
    assert result.dataset is not None
    record = source_store.load(result.dataset.dataset_id)
    target_store = HistoricalDatasetStore(tmp_path / "target")

    def fail_replace(source, target):
        raise OSError("simulated publish failure")

    monkeypatch.setattr("app.services.historical_datasets.os.link", fail_replace)
    with pytest.raises(OSError, match="simulated publish failure"):
        target_store.save(record)

    assert target_store.list().datasets == ()
    assert list(target_store.root.iterdir()) == []


def test_backtest_exact_dataset_selection_never_reads_legacy_evidence(tmp_path):
    store = HistoricalDatasetStore(tmp_path / "datasets")
    ingestion = HistoricalDatasetService(
        store=store,
        resolver=ResearchRequestService(),
        client=_Client([_payload(START, START + timedelta(minutes=15))]),
        now=lambda: NOW,
    )
    result = asyncio.run(ingestion.ingest(_request()))
    assert result.dataset is not None

    class _NoLegacyRead:
        def evidence_snapshot(self, *, symbol=None):
            raise AssertionError("dataset-selected backtests must not read or fetch another source")

    service = BacktestingService(
        evidence_owner=_NoLegacyRead(),
        resolver=ResearchRequestService(),
        dataset_store=store,
        minimum_history_bars=1,
        now=lambda: NOW,
    )
    response = service.run(BacktestRequest(
        symbol="XAU/USD",
        timeframe="15m",
        dataset_id=result.dataset.dataset_id,
        dataset_fingerprint=result.dataset.content_fingerprint,
        start_at=START,
        end_at=START + timedelta(minutes=30),
    ))

    assert response.report.status in {
        BacktestStatus.AVAILABLE,
        BacktestStatus.PARTIAL,
        BacktestStatus.INSUFFICIENT_DATA,
    }
    assert response.report.production_authorization_available is False
    assert response.report.submission_allowed is False
    assert response.report.execution_authority is False


def test_backtest_rejects_dataset_fingerprint_and_range_mismatch(tmp_path):
    store = HistoricalDatasetStore(tmp_path / "datasets")
    ingestion = HistoricalDatasetService(
        store=store,
        resolver=ResearchRequestService(),
        client=_Client([_payload(START, START + timedelta(minutes=15))]),
        now=lambda: NOW,
    )
    result = asyncio.run(ingestion.ingest(_request()))
    assert result.dataset is not None

    class _Owner:
        def evidence_snapshot(self, *, symbol=None):
            raise AssertionError("legacy evidence must not be selected")

    service = BacktestingService(
        evidence_owner=_Owner(), resolver=ResearchRequestService(), dataset_store=store, now=lambda: NOW
    )
    mismatch = service.run(BacktestRequest(
        symbol="XAU/USD", timeframe="15m", dataset_id=result.dataset.dataset_id,
        dataset_fingerprint="f" * 64,
    ))
    assert mismatch.report.status is BacktestStatus.UNAVAILABLE
    assert "HISTORICAL_DATASET_FINGERPRINT_MISMATCH" in mismatch.report.warnings
