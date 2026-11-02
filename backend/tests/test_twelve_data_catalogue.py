import asyncio
import secrets
from typing import Any

import httpx
import pytest

from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataPayloadError,
    TwelveDataProviderError,
)
from app.models.instruments import ExchangeResolution, InstrumentCategory


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _client(handler: Any) -> TwelveDataClient:
    return TwelveDataClient(
        secrets.token_urlsafe(32), transport=httpx.MockTransport(handler)
    )


def test_catalogue_methods_normalize_categories_filters_and_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("apikey ")
        if request.url.path == "/forex_pairs":
            assert dict(request.url.params) == {
                "symbol": "EUR/USD",
                "currency_base": "EUR",
                "currency_quote": "USD",
                "page": "2",
                "outputsize": "20",
            }
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "count": 1,
                    "data": [
                        {
                            "symbol": "EUR/USD",
                            "currency_group": "Major",
                            "currency_base": "EUR",
                            "currency_quote": "USD",
                        }
                    ],
                },
            )
        if request.url.path == "/commodities":
            assert dict(request.url.params) == {
                "category": "Precious Metal",
                "page": "1",
            }
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "count": 1,
                    "data": [
                        {
                            "symbol": "XAU/USD",
                            "name": "Gold Spot",
                            "category": "Precious Metal",
                            "description": "Spot price per troy ounce of gold.",
                        }
                    ],
                },
            )
        assert request.url.path == "/cryptocurrencies"
        assert dict(request.url.params) == {
            "symbol": "BTC/USD",
            "exchange": "Binance",
            "currency_base": "BTC",
            "currency_quote": "USD",
            "page": "3",
            "outputsize": "10",
        }
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "count": 1,
                "data": [
                    {
                        "symbol": "BTC/USD",
                        "currency_base": "Bitcoin",
                        "currency_quote": "US Dollar",
                        "available_exchanges": ["Binance", "Coinbase"],
                    }
                ],
            },
        )

    client = _client(handler)
    forex = _run(
        client.fetch_forex_pairs(
            symbol="eur/usd",
            currency_base="EUR",
            currency_quote="USD",
            page=2,
            outputsize=20,
        )
    )
    commodity = _run(client.fetch_commodities(category="Precious Metal"))
    crypto = _run(
        client.fetch_cryptocurrencies(
            symbol="btc/usd",
            exchange="Binance",
            currency_base="BTC",
            currency_quote="USD",
            page=3,
            outputsize=10,
        )
    )

    assert forex.category is InstrumentCategory.FOREX
    assert forex.page == 2
    assert forex.outputsize == 20
    assert forex.instruments[0].provider_symbol == "EUR/USD"
    assert forex.instruments[0].display_symbol == "EURUSD"
    assert forex.instruments[0].instrument_id == "twelve_data:forex:EUR/USD"
    assert forex.instruments[0].quote_access == "unknown"

    assert commodity.category is InstrumentCategory.COMMODITY
    assert commodity.instruments[0].provider_symbol == "XAU/USD"
    assert commodity.instruments[0].display_symbol == "XAUUSD"
    assert commodity.instruments[0].commodity_category == "Precious Metal"

    assert crypto.category is InstrumentCategory.CRYPTO
    assert crypto.instruments[0].provider_symbol == "BTC/USD"
    assert crypto.instruments[0].display_symbol == "BTCUSD"
    assert crypto.instruments[0].available_exchanges == ("Binance", "Coinbase")


def test_crypto_catalogue_preserves_exchange_ambiguity_without_selection() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={
                "status": "ok",
                "count": 1,
                "data": [
                    {
                        "symbol": "BTC/USD",
                        "available_exchanges": ["Binance", "Coinbase", "Kraken"],
                    }
                ],
            },
        )
    )

    page = _run(client.fetch_cryptocurrencies())

    assert page.instruments[0].available_exchanges == (
        "Binance",
        "Coinbase",
        "Kraken",
    )
    assert page.instruments[0].instrument_id == "twelve_data:crypto:BTC/USD"
    assert page.instruments[0].quote_access == "unknown"


def test_connector_distinguishes_identical_symbols_on_different_exchanges() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={
                "status": "ok",
                "count": 2,
                "data": [
                    {"symbol": "ABC/USD", "exchange": "ExchangeOne"},
                    {"symbol": "ABC/USD", "exchange": "ExchangeTwo"},
                ],
            },
        )
    )

    page = _run(client.fetch_cryptocurrencies())

    assert len({item.instrument_id for item in page.instruments}) == 2
    assert len({item.canonical_instrument_id for item in page.instruments}) == 2
    assert {item.exchange_id for item in page.instruments} == {
        "EXCHANGEONE",
        "EXCHANGETWO",
    }
    assert all(
        item.exchange_resolution is ExchangeResolution.RESOLVED
        for item in page.instruments
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "ok", "count": 1, "data": []},
        {"status": "ok", "count": 1, "data": [{}]},
        {"status": "ok", "count": 1, "data": [{"symbol": "BTC/USD", "available_exchanges": "Binance"}]},
    ],
)
def test_catalogue_rejects_malformed_empty_or_incomplete_responses(
    payload: dict[str, Any],
) -> None:
    client = _client(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(TwelveDataPayloadError):
        _run(client.fetch_cryptocurrencies())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"status": "error"}),
        httpx.Response(200, json={"status": "error", "message": "access denied"}),
    ],
)
def test_catalogue_maps_provider_failures_to_sanitized_errors(
    response: httpx.Response,
) -> None:
    client = _client(lambda request: response)

    with pytest.raises(TwelveDataProviderError, match="Twelve Data request failed"):
        _run(client.fetch_forex_pairs())


def test_catalogue_rejects_invalid_pagination_and_filters_without_requests() -> None:
    client = _client(lambda request: pytest.fail("request should not be made"))

    with pytest.raises(TwelveDataPayloadError, match="page is invalid"):
        _run(client.fetch_forex_pairs(page=0))
    with pytest.raises(TwelveDataPayloadError, match="outputsize is invalid"):
        _run(client.fetch_commodities(outputsize=0))
    with pytest.raises(TwelveDataPayloadError, match="exchange is invalid"):
        _run(client.fetch_cryptocurrencies(exchange=" "))
