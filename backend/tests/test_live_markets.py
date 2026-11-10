import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.connectors.twelve_data import TwelveDataClient, twelve_data_connector
from app.data.mock_data import MARKETS
from app.main import app
from app.models.schemas import Market, MarketListResponse
from app.services.market_data import (
    DASHBOARD_SYMBOLS,
    MarketDataService,
    get_market_data_service,
)


def _mock_payloads() -> dict[str, dict[str, Any]]:
    return {
        "XAU/USD": {
            "symbol": "XAU/USD",
            "name": "Gold Spot / US Dollar",
            "exchange": "Commodity",
            "currency": "USD",
            "datetime": "2026-10-29 18:00:00",
            "timestamp": 1_793_296_800,
            "open": "2740.00",
            "high": "2755.00",
            "low": "2735.00",
            "close": "2750.25",
            "previous_close": "2730.00",
            "change": "20.25",
            "percent_change": "0.7417",
            "is_market_open": True,
        },
        "EUR/USD": {
            "symbol": "EUR/USD",
            "name": "Euro / US Dollar",
            "exchange": "Forex",
            "currency": "USD",
            "datetime": "2026-10-29 18:00:00",
            "timestamp": 1_793_296_800,
            "open": "1.0820",
            "high": "1.0860",
            "low": "1.0810",
            "close": "1.0845",
            "previous_close": "1.0830",
            "change": "0.0015",
            "percent_change": "0.1385",
            "is_market_open": True,
        },
        "BTC/USD": {
            "symbol": "BTC/USD",
            "name": "Bitcoin / US Dollar",
            "exchange": "Crypto",
            "currency": "USD",
            "datetime": "2026-10-29 18:00:00",
            "timestamp": 1_793_296_800,
            "open": "71000.00",
            "high": "72500.00",
            "low": "70800.00",
            "close": "72100.50",
            "previous_close": "70500.00",
            "change": "1600.50",
            "percent_change": "2.2702",
            "is_market_open": True,
        },
    }


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _response_from_payloads(payloads: dict[str, dict[str, Any]]) -> MarketListResponse:
    client = TwelveDataClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=payloads[req.url.params["symbol"]])
        ),
    )
    return _run(MarketDataService(client=client).get_markets())


def test_market_data_service_unconfigured_returns_mock_fixtures() -> None:
    service = MarketDataService(client=None)
    response = _run(service.get_markets())

    assert response.data_source == "mock"
    assert response.count == 3
    assert len(response.markets) == 3
    assert all(m.is_mock for m in response.markets)
    assert all(m.data_source == "mock" for m in response.markets)
    assert all(m.freshness == "mock" for m in response.markets)
    assert all(m.freshness_reason == "mock" for m in response.markets)
    assert all(m.provider_is_delayed is None for m in response.markets)
    assert all(m.received_at is None for m in response.markets)
    assert [m.symbol for m in response.markets] == ["XAUUSD", "EURUSD", "BTCUSD"]
    assert response.markets[0].price == MARKETS[0].price


def test_market_data_service_configured_fetches_and_normalizes_all_three_symbols() -> None:
    requested_symbols: list[str] = []
    payloads = _mock_payloads()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/quote"
        symbol = request.url.params["symbol"]
        requested_symbols.append(symbol)
        assert request.headers["Authorization"] == "apikey test-key"
        return httpx.Response(200, json=payloads[symbol])

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    service = MarketDataService(client=client)

    response = _run(service.get_markets())

    assert set(requested_symbols) == {"XAU/USD", "EUR/USD", "BTC/USD"}
    assert response.data_source == "twelve_data"
    assert response.count == 3
    assert len(response.markets) == 3

    market_map = {m.symbol: m for m in response.markets}
    assert set(market_map.keys()) == {"XAUUSD", "EURUSD", "BTCUSD"}

    xau = market_map["XAUUSD"]
    assert xau.price == 2750.25
    assert xau.high == 2755.00
    assert xau.low == 2735.00
    assert xau.change_percent == pytest.approx(0.7417)
    assert xau.currency == "USD"
    assert xau.is_mock is False
    assert xau.data_source == "twelve_data"
    assert xau.quote_timestamp is not None
    assert xau.received_at is not None
    assert xau.quote_age_seconds is not None
    assert xau.provider_is_delayed is None

    eur = market_map["EURUSD"]
    assert eur.price == 1.0845
    assert eur.high == 1.0860
    assert eur.low == 1.0810
    assert eur.is_mock is False
    assert eur.data_source == "twelve_data"

    btc = market_map["BTCUSD"]
    assert btc.price == 72100.50
    assert btc.is_mock is False
    assert btc.data_source == "twelve_data"


def test_market_data_service_falls_back_to_mock_on_rate_limit_or_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"status": "error", "message": "Rate limit exceeded"})

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    service = MarketDataService(client=client)

    response = _run(service.get_markets())

    assert response.data_source == "mock"
    assert response.count == 3
    assert all(m.is_mock for m in response.markets)
    assert all(m.data_source == "mock" for m in response.markets)


def test_market_data_service_falls_back_to_mock_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Request timed out")

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    service = MarketDataService(client=client)

    response = _run(service.get_markets())

    assert response.data_source == "mock"
    assert all(m.is_mock for m in response.markets)


def test_market_data_service_falls_back_to_mock_on_single_symbol_failure() -> None:
    payloads = _mock_payloads()

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        if symbol == "BTC/USD":
            return httpx.Response(404, json={"status": "error", "message": "Symbol not found"})
        return httpx.Response(200, json=payloads[symbol])

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    service = MarketDataService(client=client)

    response = _run(service.get_markets())

    # Must NOT return mixed 2 live + 1 mock; falls back to pure mock set
    assert response.data_source == "mock"
    assert all(m.is_mock for m in response.markets)
    assert all(m.data_source == "mock" for m in response.markets)


def test_market_data_service_marks_missing_provider_delay_flag_as_unknown() -> None:
    now = datetime.now(UTC)

    live_payload = _mock_payloads()
    for item in live_payload.values():
        item["timestamp"] = int((now - timedelta(seconds=15)).timestamp())
        item["is_market_open"] = True

    res_live = _response_from_payloads(live_payload)
    assert all(m.freshness == "live" for m in res_live.markets)
    assert all(m.freshness_reason == "fresh_quote" for m in res_live.markets)
    assert all(m.provider_is_delayed is None for m in res_live.markets)
    assert all(m.received_at is not None for m in res_live.markets)


def test_market_data_service_marks_provider_delayed_quotes_separately() -> None:
    now = datetime.now(UTC)
    provider_delayed_payload = _mock_payloads()
    for item in provider_delayed_payload.values():
        item["timestamp"] = int((now - timedelta(seconds=15)).timestamp())
        item["is_market_open"] = True
        item["is_delayed"] = True

    response = _response_from_payloads(provider_delayed_payload)

    assert all(m.freshness == "delayed" for m in response.markets)
    assert all(m.freshness_reason == "provider_delayed" for m in response.markets)
    assert all(m.provider_is_delayed is True for m in response.markets)


def test_market_data_service_marks_age_delayed_quotes_separately() -> None:
    now = datetime.now(UTC)

    delayed_payload = _mock_payloads()
    for item in delayed_payload.values():
        item["timestamp"] = int((now - timedelta(minutes=5)).timestamp())
        item["is_market_open"] = True

    response = _response_from_payloads(delayed_payload)

    assert all(m.freshness == "delayed" for m in response.markets)
    assert all(m.freshness_reason == "quote_age" for m in response.markets)
    assert all(m.provider_is_delayed is None for m in response.markets)


def test_market_data_service_marks_stale_quotes_by_age() -> None:
    now = datetime.now(UTC)
    stale_payload = _mock_payloads()
    for item in stale_payload.values():
        item["timestamp"] = int((now - timedelta(minutes=30)).timestamp())
        item["is_market_open"] = True

    response = _response_from_payloads(stale_payload)

    assert all(m.freshness == "stale" for m in response.markets)
    assert all(m.freshness_reason == "stale_quote_age" for m in response.markets)


def test_market_data_service_marks_closed_market_quotes_stale() -> None:
    now = datetime.now(UTC)

    closed_payload = _mock_payloads()
    for item in closed_payload.values():
        item["timestamp"] = int((now - timedelta(seconds=15)).timestamp())
        item["is_market_open"] = False

    response = _response_from_payloads(closed_payload)

    assert all(m.freshness == "stale" for m in response.markets)
    assert all(m.freshness_reason == "market_closed" for m in response.markets)


def test_schema_rejects_mixed_or_misleading_data_sources() -> None:
    mock_market = Market(
        symbol="XAUUSD",
        name="Gold / US Dollar",
        price=100.0,
        change_percent=0.5,
        high=101.0,
        low=99.0,
        is_mock=True,
        data_source="mock",
    )
    live_market = Market(
        symbol="EURUSD",
        name="Euro / US Dollar",
        price=1.08,
        change_percent=0.1,
        high=1.09,
        low=1.07,
        is_mock=False,
        data_source="twelve_data",
    )

    # Cannot mix mock and live markets in a single response
    with pytest.raises(ValidationError, match="all markets must match"):
        MarketListResponse(data_source="twelve_data", count=2, markets=[mock_market, live_market])

    with pytest.raises(ValidationError, match="all markets must match"):
        MarketListResponse(data_source="mock", count=2, markets=[mock_market, live_market])

    # Cannot label live market as mock or vice versa
    with pytest.raises(ValidationError, match="mock data source must set is_mock=True"):
        Market(
            symbol="XAUUSD",
            name="Gold / US Dollar",
            price=100.0,
            change_percent=0.5,
            high=101.0,
            low=99.0,
            is_mock=False,
            data_source="mock",
        )

    with pytest.raises(ValidationError, match="twelve_data source must set is_mock=False"):
        Market(
            symbol="XAUUSD",
            name="Gold / US Dollar",
            price=100.0,
            change_percent=0.5,
            high=101.0,
            low=99.0,
            is_mock=True,
            data_source="twelve_data",
        )


def _get(path: str) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return _run(send_request())


def test_api_markets_endpoint_integration_with_configured_provider() -> None:
    payloads = _mock_payloads()

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        return httpx.Response(200, json=payloads[symbol])

    client = TwelveDataClient("test-key", transport=httpx.MockTransport(handler))
    service = MarketDataService(client=client)

    app.dependency_overrides[get_market_data_service] = lambda: service
    try:
        resp = _get("/api/markets")
    finally:
        app.dependency_overrides.pop(get_market_data_service, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["data_source"] == "twelve_data"
    assert data["count"] == 3
    assert len(data["markets"]) == 3
    assert all(not m["is_mock"] for m in data["markets"])
    assert all(m["data_source"] == "twelve_data" for m in data["markets"])


def test_production_authorization_remains_disabled_while_market_data_runs() -> None:
    readiness_resp = _get("/api/readiness")
    markets_resp = _get("/api/markets")

    assert readiness_resp.status_code == 200
    readiness = readiness_resp.json()
    assert readiness["status"] == "blocked"
    assert readiness["production_authorization_available"] is False
    assert readiness["submission_allowed"] is False
    assert readiness["execution_authority"] is False

    assert markets_resp.status_code == 200
