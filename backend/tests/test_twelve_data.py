import asyncio
import secrets
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from app import config as config_module
from app.agents import create_friday_core
from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConfigurationError,
    TwelveDataPayloadError,
    TwelveDataProviderError,
    twelve_data_connector,
)
from app.ingestion import IngestionService, MarketSnapshotService
from app.ingestion.contracts import SourceConnector
from app.main import app
from app.models.events import AssetClass, EventType, NormalizedDataEvent, SourceTransport
from app.services.twelve_data import get_twelve_data_snapshot_service


def _quote_payload() -> dict[str, Any]:
    return {
        "symbol": "XAU/USD",
        "name": "Gold Spot / US Dollar",
        "exchange": "Commodity",
        "currency": "USD",
        "datetime": "2026-09-29 12:00:00",
        "timestamp": 1_790_683_200,
        "open": "2640.00",
        "high": "2650.00",
        "low": "2638.00",
        "close": "2648.32",
        "volume": None,
        "previous_close": "2635.00",
        "change": "13.32",
        "percent_change": "0.5055",
        "is_market_open": True,
    }


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _post(path: str) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(path)

    return _run(send_request())


def _mock_client(
    handler: Any,
    api_key: str,
) -> TwelveDataClient:
    return TwelveDataClient(api_key, transport=httpx.MockTransport(handler))


def test_twelve_data_client_fetches_a_market_snapshot() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/quote"
        assert request.url.params["symbol"] == "XAU/USD"
        authorization_matches = secrets.compare_digest(
            request.headers["Authorization"],
            f"apikey {api_key}",
        )
        key_is_in_url = api_key in str(request.url)
        assert authorization_matches
        assert not key_is_in_url
        return httpx.Response(200, json=_quote_payload())

    payload = _run(_mock_client(handler, api_key).fetch_snapshot("xau/usd"))

    assert payload["symbol"] == "XAU/USD"
    assert payload["close"] == "2648.32"


def test_twelve_data_connector_normalizes_provider_output() -> None:
    event = twelve_data_connector.normalize(_quote_payload())

    assert isinstance(event, NormalizedDataEvent)
    assert event.source.source_id == "twelve_data"
    assert event.source.transport is SourceTransport.API
    assert event.event_type is EventType.MARKET_QUOTE
    assert event.asset_class is AssetClass.COMMODITY
    assert event.symbol == "XAU/USD"
    assert event.price == 2648.32
    assert event.open == 2640.0
    assert event.high == 2650.0
    assert event.low == 2638.0
    assert event.close == 2648.32
    assert event.timestamp.isoformat() == "2026-09-29T12:00:00+00:00"
    assert event.attributes["previous_close"] == 2635.0
    assert isinstance(twelve_data_connector, SourceConnector)


@pytest.mark.parametrize("configured_value", [None, "", "   "])
def test_twelve_data_client_rejects_missing_or_blank_api_key(
    monkeypatch: Any,
    configured_value: str | None,
) -> None:
    if configured_value is None:
        monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TWELVE_DATA_API_KEY", configured_value)

    with pytest.raises(TwelveDataConfigurationError, match="not configured"):
        TwelveDataClient.from_environment(load_env_file=False)


def test_twelve_data_client_is_configured_from_the_process_environment(
    monkeypatch: Any,
) -> None:
    api_key = secrets.token_urlsafe(32)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", api_key)

    client = TwelveDataClient.from_environment(load_env_file=False)

    representation_exposes_key = api_key in repr(client)
    assert isinstance(client, TwelveDataClient)
    assert not representation_exposes_key


def test_twelve_data_client_uses_the_backend_env_file_as_a_fallback(
    monkeypatch: Any,
) -> None:
    api_key = secrets.token_urlsafe(32)
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.setattr(
        config_module,
        "dotenv_values",
        lambda path: {"TWELVE_DATA_API_KEY": api_key},
    )

    client = TwelveDataClient.from_environment()

    representation_exposes_key = api_key in repr(client)
    assert isinstance(client, TwelveDataClient)
    assert not representation_exposes_key


def test_twelve_data_client_handles_invalid_credentials_without_exposing_them() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "code": 401, "message": "Invalid API key"},
        )

    with pytest.raises(TwelveDataProviderError) as error:
        _run(_mock_client(handler, api_key).fetch_snapshot("XAU/USD"))

    error_exposes_key = api_key in str(error.value)
    assert not error_exposes_key


def test_twelve_data_client_handles_provider_errors() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"status": "error"})

    with pytest.raises(TwelveDataProviderError, match="request failed"):
        _run(_mock_client(handler, api_key).fetch_snapshot("XAU/USD"))


def test_twelve_data_client_rejects_malformed_json() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(TwelveDataPayloadError, match="malformed market data"):
        _run(_mock_client(handler, api_key).fetch_snapshot("XAU/USD"))


def test_twelve_data_connector_rejects_malformed_responses() -> None:
    malformed_payload = _quote_payload()
    malformed_payload["close"] = "not-a-number"

    with pytest.raises(TwelveDataPayloadError, match="price must be numeric"):
        twelve_data_connector.normalize(malformed_payload)


def test_twelve_data_snapshot_is_ingested_by_friday_core() -> None:
    class StubClient:
        async def fetch_snapshot(self, symbol: str) -> Mapping[str, Any]:
            assert symbol == "XAU/USD"
            return _quote_payload()

    core = create_friday_core()
    service = MarketSnapshotService(
        client=StubClient(),
        connector=twelve_data_connector,
        ingestion=IngestionService(consumer=core),
    )

    event = _run(service.snapshot("XAU/USD"))

    assert core.recent_market_events == (event,)
    assert event.source.source_id == "twelve_data"


def test_twelve_data_api_returns_normalized_data_without_the_api_key() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _quote_payload()
        payload["api_key"] = api_key
        return httpx.Response(200, json=payload)

    core = create_friday_core()
    service = MarketSnapshotService(
        client=_mock_client(handler, api_key),
        connector=twelve_data_connector,
        ingestion=IngestionService(consumer=core),
    )
    app.dependency_overrides[get_twelve_data_snapshot_service] = lambda: service
    try:
        response = _post("/api/market-data/twelve-data/snapshot?symbol=XAU%2FUSD")
    finally:
        app.dependency_overrides.pop(get_twelve_data_snapshot_service, None)

    assert response.status_code == 200
    assert response.json()["source"]["source_id"] == "twelve_data"
    assert response.json()["symbol"] == "XAU/USD"
    response_exposes_key = api_key in response.text
    assert not response_exposes_key
    assert "api_key" not in response.json()["raw_payload"]
    assert core.recent_market_events[0].source.source_id == "twelve_data"


def test_twelve_data_api_fails_closed_without_configuration(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.setattr(config_module, "dotenv_values", lambda path: {})

    response = _post("/api/market-data/twelve-data/snapshot")

    assert response.status_code == 503
    assert response.json() == {"detail": "Twelve Data is not configured."}
