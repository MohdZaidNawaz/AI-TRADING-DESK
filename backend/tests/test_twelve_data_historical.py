import asyncio
import secrets
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.agents import create_friday_core
from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataPayloadError,
    TwelveDataProviderError,
    twelve_data_historical_connector,
    twelve_data_historical_interval,
)
from app.models.research_requests import EvidenceAvailabilityStatus, HistoricalRetrievalStatus
from app.models.schemas import AnalyzeRequest
from app.main import app
from app.services.research_requests import ResearchRequestService
from app.services.twelve_data_historical import (
    TwelveDataHistoricalService,
    get_twelve_data_historical_service,
)


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _post_analyze(payload: dict[str, Any]) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.post("/api/analyze", json=payload)

    return _run(send())


def _value(at: str, *, high: str = "2651", volume: str | None = None) -> dict[str, Any]:
    return {
        "datetime": at,
        "open": "2645",
        "high": high,
        "low": "2640",
        "close": "2648",
        "volume": volume,
    }


def _payload(
    values: list[dict[str, Any]] | None = None,
    *,
    symbol: str = "XAU/USD",
    interval: str = "15min",
) -> dict[str, Any]:
    return {
        "meta": {
            "symbol": symbol,
            "interval": interval,
            "exchange": "Commodity",
            "exchange_timezone": "UTC",
        },
        "values": values
        if values is not None
        else [
            _value("2026-11-03 11:15:00"),
            _value("2026-11-03 11:30:00"),
            _value("2026-11-03 11:45:00"),
        ],
    }


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        ("1m", "1min"),
        ("5m", "5min"),
        ("15m", "15min"),
        ("30m", "30min"),
        ("1h", "1h"),
        ("4h", "4h"),
        ("1day", "1day"),
        ("1week", "1week"),
    ],
)
def test_historical_interval_mapping(timeframe: str, expected: str) -> None:
    assert twelve_data_historical_interval(timeframe) == expected


def test_unsupported_historical_interval_is_rejected() -> None:
    with pytest.raises(TwelveDataPayloadError, match="not supported"):
        twelve_data_historical_interval("2h")


def test_client_fetches_one_bounded_time_series_without_exposing_key() -> None:
    key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/time_series"
        assert request.url.params["symbol"] == "XAU/USD"
        assert request.url.params["interval"] == "15min"
        assert request.url.params["outputsize"] == "3"
        assert request.url.params["timezone"] == "UTC"
        assert request.url.params["order"] == "ASC"
        assert key not in str(request.url)
        assert request.headers["Authorization"] == f"apikey {key}"
        return httpx.Response(200, json=_payload())

    client = TwelveDataClient(key, transport=httpx.MockTransport(handler))
    result = _run(client.fetch_time_series("xau/usd", "15m", outputsize=3))

    assert result["meta"]["symbol"] == "XAU/USD"


def test_client_encodes_incremental_start_in_documented_utc_format() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["start_date"] == "2026-11-03T11:45:00"
        assert request.url.params["timezone"] == "UTC"
        return httpx.Response(200, json=_payload())

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    _run(
        client.fetch_time_series(
            "XAU/USD",
            "15m",
            outputsize=3,
            start_date=datetime(2026, 11, 3, 11, 45, tzinfo=UTC),
        )
    )


def test_client_encodes_bounded_end_and_exact_exchange_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["start_date"] == "2026-11-01T00:00:00"
        assert request.url.params["end_date"] == "2026-11-03T12:00:00"
        assert request.url.params["exchange"] == "COMMODITY"
        return httpx.Response(200, json=_payload())

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    _run(
        client.fetch_time_series(
            "XAU/USD",
            "15m",
            outputsize=3,
            start_date=datetime(2026, 11, 1, tzinfo=UTC),
            end_date=NOW,
            exchange="COMMODITY",
        )
    )


def test_client_timeout_is_bounded_and_sanitized() -> None:
    key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout with no secret", request=request)

    client = TwelveDataClient(key, transport=httpx.MockTransport(handler))
    with pytest.raises(TwelveDataProviderError) as error:
        _run(client.fetch_time_series("XAU/USD", "15m", outputsize=3))

    assert error.value.category == "timeout"
    assert key not in str(error.value)


def test_client_http_429_is_reported_without_retry_or_secret() -> None:
    key = secrets.token_urlsafe(32)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"status": "error", "message": key})

    client = TwelveDataClient(key, transport=httpx.MockTransport(handler))
    with pytest.raises(TwelveDataProviderError) as error:
        _run(client.fetch_time_series("XAU/USD", "15m", outputsize=3))

    assert error.value.category == "rate_limit"
    assert error.value.status_code == 429
    assert key not in str(error.value)
    assert calls == 1


def test_valid_candles_are_normalized_with_source_timestamp_and_optional_volume() -> None:
    batch = twelve_data_historical_connector.normalize(
        _payload(),
        expected_symbol="XAU/USD",
        timeframe="15m",
        requested_count=3,
        retrieved_at=NOW,
    )

    assert len(batch.bars) == 3
    assert batch.bars[0].source_timestamp == "2026-11-03 11:15:00"
    assert batch.bars[0].opened_at.isoformat() == "2026-11-03T11:15:00+00:00"
    assert batch.bars[0].volume is None
    assert batch.bars[0].volume_kind.value == "unavailable"
    assert batch.bars[-1].state.value == "completed"
    assert not batch.partial


def test_intraday_utc_request_accepts_missing_exchange_timezone() -> None:
    payload = _payload()
    del payload["meta"]["exchange_timezone"]

    batch = twelve_data_historical_connector.normalize(
        payload,
        expected_symbol="XAU/USD",
        timeframe="15m",
        requested_count=3,
        retrieved_at=NOW,
        requested_timezone="UTC",
    )

    assert batch.requested_timezone == "UTC"
    assert batch.source_timezone == "UTC"
    assert batch.bars[0].source_timestamp == "2026-11-03 11:15:00"
    assert batch.bars[0].opened_at == datetime(2026, 11, 3, 11, 15, tzinfo=UTC)
    assert batch.exchange_metadata_available is False
    assert "HISTORICAL_EXCHANGE_METADATA_UNAVAILABLE" in batch.warning_codes
    assert batch.partial is False


def test_intraday_missing_exchange_timezone_without_explicit_utc_is_rejected() -> None:
    payload = _payload()
    del payload["meta"]["exchange_timezone"]

    with pytest.raises(TwelveDataPayloadError, match="incomplete historical timezone"):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe="15m",
            requested_count=3,
            retrieved_at=NOW,
        )


def test_intraday_invalid_exchange_timezone_is_rejected_even_for_utc_request() -> None:
    payload = _payload()
    payload["meta"]["exchange_timezone"] = "Not/A_Timezone"

    with pytest.raises(TwelveDataPayloadError, match="unsupported historical timezone"):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe="15m",
            requested_count=3,
            retrieved_at=NOW,
            requested_timezone="UTC",
        )


@pytest.mark.parametrize(("timeframe", "interval"), [("1day", "1day"), ("1week", "1week")])
def test_daily_and_weekly_missing_exchange_timezone_are_rejected(
    timeframe: str,
    interval: str,
) -> None:
    payload = _payload([_value("2026-11-03 00:00:00")], interval=interval)
    del payload["meta"]["exchange_timezone"]

    with pytest.raises(TwelveDataPayloadError, match="incomplete historical timezone"):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe=timeframe,
            requested_count=1,
            retrieved_at=datetime(2026, 11, 5, tzinfo=UTC),
            requested_timezone="UTC",
        )


def test_missing_exchange_is_retained_as_unknown_metadata() -> None:
    payload = _payload()
    del payload["meta"]["exchange"]

    batch = twelve_data_historical_connector.normalize(
        payload,
        expected_symbol="XAU/USD",
        timeframe="15m",
        requested_count=3,
        retrieved_at=NOW,
        requested_timezone="UTC",
    )

    assert batch.venue is None
    assert batch.exchange_metadata_available is False
    assert "HISTORICAL_EXCHANGE_METADATA_UNAVAILABLE" in batch.warning_codes
    assert batch.partial is False


def test_daily_bar_uses_documented_exchange_timezone_before_utc_normalization() -> None:
    payload = _payload(
        [_value("2026-11-03 00:00:00")],
        interval="1day",
    )
    payload["meta"]["exchange_timezone"] = "UTC"

    batch = twelve_data_historical_connector.normalize(
        payload,
        expected_symbol="XAU/USD",
        timeframe="1day",
        requested_count=1,
        retrieved_at=datetime(2026, 11, 5, tzinfo=UTC),
    )

    assert batch.source_timezone == "UTC"
    assert batch.bars[0].source_timestamp == "2026-11-03 00:00:00"
    assert batch.bars[0].opened_at == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def test_descending_provider_order_is_validated_then_normalized_ascending() -> None:
    payload = _payload()
    payload["values"] = list(reversed(payload["values"]))

    batch = twelve_data_historical_connector.normalize(
        payload,
        expected_symbol="XAU/USD",
        timeframe="15m",
        requested_count=3,
        retrieved_at=NOW,
    )

    assert [bar.opened_at.minute for bar in batch.bars] == [15, 30, 45]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["values"][0].update(open="not-a-number"),
        lambda payload: payload["values"][0].update(high="2639"),
    ],
)
def test_invalid_numeric_or_high_low_candle_is_rejected(mutate: Any) -> None:
    payload = _payload()
    mutate(payload)

    with pytest.raises(TwelveDataPayloadError):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe="15m",
            requested_count=3,
            retrieved_at=NOW,
        )


def test_duplicate_timestamps_are_rejected() -> None:
    payload = _payload()
    payload["values"][1]["datetime"] = payload["values"][0]["datetime"]

    with pytest.raises(TwelveDataPayloadError, match="duplicate"):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe="15m",
            requested_count=3,
            retrieved_at=NOW,
        )


def test_mixed_out_of_order_candles_are_rejected() -> None:
    payload = _payload(
        [
            _value("2026-11-03 11:30:00"),
            _value("2026-11-03 11:00:00"),
            _value("2026-11-03 11:15:00"),
        ]
    )

    with pytest.raises(TwelveDataPayloadError, match="out-of-order"):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe="15m",
            requested_count=3,
            retrieved_at=NOW,
        )


def test_missing_intervals_and_short_response_are_partial_without_fabrication() -> None:
    batch = twelve_data_historical_connector.normalize(
        _payload(
            [
                _value("2026-11-03 11:00:00"),
                _value("2026-11-03 11:30:00"),
            ]
        ),
        expected_symbol="XAU/USD",
        timeframe="15m",
        requested_count=3,
        retrieved_at=NOW,
    )

    assert len(batch.bars) == 2
    assert batch.missing_interval_count == 1
    assert batch.partial
    assert "HISTORICAL_INTERVALS_MISSING" in batch.warning_codes
    assert "HISTORICAL_RESPONSE_SHORT" in batch.warning_codes


def test_current_candle_is_explicitly_provisional() -> None:
    batch = twelve_data_historical_connector.normalize(
        _payload([_value("2026-11-03 12:00:00")]),
        expected_symbol="XAU/USD",
        timeframe="15m",
        requested_count=1,
        retrieved_at=NOW,
    )

    assert batch.bars[0].state.value == "provisional"
    assert batch.partial
    assert "HISTORICAL_PROVISIONAL_BAR_PRESENT" in batch.warning_codes


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload["meta"].update(symbol="XAG/USD"),
        lambda payload: payload["meta"].update(interval="1h"),
        lambda payload: payload["meta"].update(exchange_timezone="Not/A_Timezone"),
    ],
)
def test_unexpected_subject_interval_or_timezone_is_rejected(change: Any) -> None:
    payload = _payload()
    change(payload)

    with pytest.raises(TwelveDataPayloadError):
        twelve_data_historical_connector.normalize(
            payload,
            expected_symbol="XAU/USD",
            timeframe="15m",
            requested_count=3,
            retrieved_at=NOW,
        )


def test_empty_response_is_unavailable_and_never_synthetic() -> None:
    core = create_friday_core()

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return _payload([])

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    diagnostic = _run(
        service.prepare(AnalyzeRequest(query="Analyse XAUUSD on 15m", symbol=None))
    )

    assert diagnostic.status is HistoricalRetrievalStatus.UNAVAILABLE
    assert diagnostic.validated_count == 0
    assert core.evidence_snapshot(symbol="XAU/USD").market_series == ()


def test_service_accepts_intraday_utc_payload_without_exchange_metadata() -> None:
    core = create_friday_core()
    payload = _payload()
    del payload["meta"]["exchange_timezone"]
    del payload["meta"]["exchange"]

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return payload

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    diagnostic = _run(
        service.prepare(
            AnalyzeRequest(
                query="Analyse XAUUSD on 15m for the last 3 bars",
                symbol=None,
            )
        )
    )

    assert diagnostic.status is HistoricalRetrievalStatus.RETRIEVED
    assert diagnostic.validated_count == 3
    assert diagnostic.partial is False
    assert "HISTORICAL_EXCHANGE_METADATA_UNAVAILABLE" in diagnostic.reason_codes
    series = core.evidence_snapshot(symbol="XAU/USD").market_series
    assert len(series) == 1
    assert series[0].provenance.source_id == "twelve_data"
    assert series[0].bars[0].source_timestamp == "2026-11-03 11:15:00"


@pytest.mark.parametrize(
    ("exception", "status", "category"),
    [
        (
            TwelveDataProviderError(category="timeout"),
            HistoricalRetrievalStatus.TIMEOUT,
            "timeout",
        ),
        (
            TwelveDataProviderError(category="rate_limit", status_code=429),
            HistoricalRetrievalStatus.RATE_LIMITED,
            "rate_limit",
        ),
    ],
)
def test_provider_failures_are_sanitized(
    exception: Exception,
    status: HistoricalRetrievalStatus,
    category: str,
) -> None:
    core = create_friday_core()

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise exception

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    diagnostic = _run(
        service.prepare(AnalyzeRequest(query="Analyse XAUUSD on 15m", symbol=None))
    )

    assert diagnostic.status is status
    assert diagnostic.error_category == category
    assert "apikey" not in diagnostic.model_dump_json().lower()
    assert core.evidence_snapshot(symbol="XAU/USD").market_series == ()


def test_rate_limit_stops_followup_provider_requests() -> None:
    core = create_friday_core()

    class Client:
        calls = 0

        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise TwelveDataProviderError(category="rate_limit", status_code=429)

    client = Client()
    service = TwelveDataHistoricalService(core=core, client=client, now=lambda: NOW)
    request = AnalyzeRequest(query="Analyse XAUUSD on 15m", symbol=None)

    first = _run(service.prepare(request))
    second = _run(service.prepare(request))

    assert first.status is HistoricalRetrievalStatus.RATE_LIMITED
    assert second.status is HistoricalRetrievalStatus.RATE_LIMITED
    assert client.calls == 1


def test_unsupported_symbol_does_not_call_provider() -> None:
    core = create_friday_core()

    class Client:
        calls = 0

        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            return _payload(symbol="BTC/USD")

    client = Client()
    service = TwelveDataHistoricalService(core=core, client=client, now=lambda: NOW)
    diagnostic = _run(
        service.prepare(AnalyzeRequest(query="Analyse BTCUSD on 1h", symbol=None))
    )

    assert diagnostic.status is HistoricalRetrievalStatus.UNSUPPORTED
    assert client.calls == 0


def test_cache_is_reused_and_duplicate_bars_are_not_added() -> None:
    core = create_friday_core()

    class Client:
        calls = 0

        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            return _payload()

    client = Client()
    service = TwelveDataHistoricalService(core=core, client=client, now=lambda: NOW)
    request = AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 3 bars", symbol=None)

    first = _run(service.prepare(request))
    second = _run(service.prepare(request))
    snapshot = core.evidence_snapshot(symbol="XAU/USD")

    assert first.status is HistoricalRetrievalStatus.RETRIEVED
    assert second.status is HistoricalRetrievalStatus.CACHE_HIT
    assert client.calls == 1
    assert len(snapshot.market_series) == 1
    assert snapshot.market_series[0].bar_count == 3


def test_stale_complete_cache_uses_documented_incremental_start_date() -> None:
    core = create_friday_core()

    class InitialClient:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return _payload()

    request = AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 3 bars")
    initial = TwelveDataHistoricalService(
        core=core,
        client=InitialClient(),
        now=lambda: NOW,
    )
    _run(initial.prepare(request))

    class IncrementalClient:
        start_date: datetime | None = None

        async def fetch_time_series(
            self,
            symbol: str,
            interval: str,
            *,
            outputsize: int,
            start_date: datetime | None = None,
        ) -> dict[str, Any]:
            self.start_date = start_date
            return _payload(
                [
                    _value("2026-11-03 12:00:00"),
                    _value("2026-11-03 12:15:00"),
                ]
            )

    client = IncrementalClient()
    refreshed = TwelveDataHistoricalService(
        core=core,
        client=client,
        now=lambda: datetime(2026, 11, 3, 13, 0, tzinfo=UTC),
    )
    diagnostic = _run(refreshed.prepare(request))

    assert client.start_date == datetime(2026, 11, 3, 11, 45, tzinfo=UTC)
    assert diagnostic.status is HistoricalRetrievalStatus.RETRIEVED
    assert diagnostic.validated_count == 5
    assert diagnostic.partial is False
    assert "HISTORICAL_INCREMENTAL_REFRESH" in diagnostic.reason_codes


def test_retrieved_bars_drive_truthful_evidence_without_changing_mock_mode() -> None:
    core = create_friday_core(
        research_request_service=ResearchRequestService(now=lambda: NOW)
    )

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return _payload()

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    request = AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 3 bars", symbol=None)
    diagnostic = _run(service.prepare(request))
    response = core.analyze(request, historical_retrieval=diagnostic)
    context = response.routing.research_context

    assert context is not None
    assert response.mode == "deterministic_mock"
    assert context.evidence.historical_ohlcv.status is EvidenceAvailabilityStatus.AVAILABLE
    assert context.evidence.historical_ohlcv.record_count == 3
    assert context.evidence.historical_ohlcv.provider_id == "twelve_data"
    assert context.evidence.current_quote.status is EvidenceAvailabilityStatus.UNKNOWN
    assert context.evidence.news.status is EvidenceAvailabilityStatus.UNKNOWN
    assert context.production_authorization_available is False
    assert context.submission_allowed is False
    assert context.execution_authority is False


def test_gapped_historical_evidence_is_partial_with_real_coverage() -> None:
    core = create_friday_core(
        research_request_service=ResearchRequestService(now=lambda: NOW)
    )

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return _payload(
                [
                    _value("2026-11-03 11:00:00"),
                    _value("2026-11-03 11:30:00"),
                ]
            )

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    request = AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 3 bars")
    diagnostic = _run(service.prepare(request))
    historical = core.analyze(
        request,
        historical_retrieval=diagnostic,
    ).routing.research_context.evidence.historical_ohlcv

    assert historical.status is EvidenceAvailabilityStatus.PARTIAL
    assert historical.record_count == 2
    assert historical.missing_interval_count == 1
    assert historical.coverage_start == datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
    assert historical.last_source_candle_at == datetime(2026, 11, 3, 11, 30, tzinfo=UTC)
    assert "HISTORICAL_INTERVALS_MISSING" in historical.reason_codes


def test_historical_freshness_uses_last_completed_source_candle() -> None:
    evaluated_at = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)
    core = create_friday_core(
        research_request_service=ResearchRequestService(now=lambda: evaluated_at)
    )

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return _payload()

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    request = AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 3 bars")
    diagnostic = _run(service.prepare(request))
    historical = core.analyze(
        request,
        historical_retrieval=diagnostic,
    ).routing.research_context.evidence.historical_ohlcv

    assert historical.status is EvidenceAvailabilityStatus.STALE
    assert historical.last_source_candle_at == datetime(2026, 11, 3, 11, 45, tzinfo=UTC)
    assert "HISTORICAL_OHLCV_STALE" in historical.reason_codes


def test_malformed_batch_never_overwrites_valid_cache() -> None:
    core = create_friday_core()

    class Client:
        calls = 0

        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return _payload()
            invalid = _payload()
            invalid["values"][0]["open"] = "invalid"
            return invalid

    client = Client()
    service = TwelveDataHistoricalService(core=core, client=client, now=lambda: NOW)
    initial = AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 3 bars", symbol=None)
    _run(service.prepare(initial))
    before = core.evidence_snapshot(symbol="XAU/USD").market_series[0].fingerprint

    # A larger lookback forces one refresh; validation fails before ingestion.
    result = _run(
        service.prepare(
            AnalyzeRequest(query="Analyse XAUUSD on 15m for the last 4 bars", symbol=None)
        )
    )
    after = core.evidence_snapshot(symbol="XAU/USD").market_series[0].fingerprint

    assert result.status is HistoricalRetrievalStatus.INVALID_RESPONSE
    assert before == after


def test_analyze_api_exposes_mocked_historical_evidence_without_contract_breakage() -> None:
    # Import the module explicitly because the routes package deliberately has no exports.
    import importlib

    route_module = importlib.import_module("app.api.routes.analyze")
    core = route_module.friday_core

    class Client:
        async def fetch_time_series(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return _payload()

    service = TwelveDataHistoricalService(core=core, client=Client(), now=lambda: NOW)
    app.dependency_overrides[get_twelve_data_historical_service] = lambda: service
    try:
        response = _post_analyze(
            {"query": "Analyse XAUUSD on 15m for the last 3 bars"}
        )
    finally:
        app.dependency_overrides.pop(get_twelve_data_historical_service, None)

    assert response.status_code == 200
    payload = response.json()
    historical = payload["routing"]["research_context"]["evidence"]["historical_ohlcv"]
    assert payload["mode"] == "deterministic_mock"
    assert historical["provider_id"] == "twelve_data"
    assert historical["record_count"] == 3
    assert payload["routing"]["research_context"]["production_authorization_available"] is False
