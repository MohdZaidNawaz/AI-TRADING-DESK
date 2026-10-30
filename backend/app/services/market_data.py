"""Dashboard market data service supporting live Twelve Data quotes and safe mock fallback."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Sequence

from app.config import get_twelve_data_api_key
from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConfigurationError,
    TwelveDataConnector,
    twelve_data_connector,
)
from app.data.mock_data import MARKETS
from app.models.schemas import Market, MarketListResponse

DASHBOARD_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("XAUUSD", "XAU/USD", "Gold / US Dollar"),
    ("EURUSD", "EUR/USD", "Euro / US Dollar"),
    ("BTCUSD", "BTC/USD", "Bitcoin / US Dollar"),
)


class MarketDataService:
    """Provider-backed dashboard market data service with safe mock fallback."""

    def __init__(
        self,
        *,
        client: TwelveDataClient | None = None,
        connector: TwelveDataConnector | None = None,
        symbols: Sequence[tuple[str, str, str]] = DASHBOARD_SYMBOLS,
    ) -> None:
        self._client = client
        self._connector = connector or twelve_data_connector
        self._symbols = tuple(symbols)

    async def get_markets(self) -> MarketListResponse:
        if self._client is None:
            return MarketListResponse(
                data_source="mock",
                count=len(MARKETS),
                markets=MARKETS,
            )

        try:
            results = await asyncio.gather(
                *(
                    self._client.fetch_snapshot(provider_sym)
                    for _, provider_sym, _ in self._symbols
                ),
                return_exceptions=True,
            )
        except Exception:
            return MarketListResponse(
                data_source="mock",
                count=len(MARKETS),
                markets=MARKETS,
            )

        if any(isinstance(res, (Exception, BaseException)) for res in results):
            return MarketListResponse(
                data_source="mock",
                count=len(MARKETS),
                markets=MARKETS,
            )

        now = datetime.now(UTC)
        live_markets: list[Market] = []

        for (dash_sym, _, default_name), payload in zip(
            self._symbols, results, strict=True
        ):
            try:
                if not isinstance(payload, dict):
                    return MarketListResponse(
                        data_source="mock", count=len(MARKETS), markets=MARKETS
                    )
                event = self._connector.normalize(payload)
            except Exception:
                return MarketListResponse(
                    data_source="mock", count=len(MARKETS), markets=MARKETS
                )

            if event.price is None or event.price <= 0:
                return MarketListResponse(
                    data_source="mock", count=len(MARKETS), markets=MARKETS
                )

            quote_ts = event.timestamp
            if quote_ts.tzinfo is None:
                quote_ts = quote_ts.replace(tzinfo=UTC)
            else:
                quote_ts = quote_ts.astimezone(UTC)

            age_seconds = max(0.0, round((now - quote_ts).total_seconds(), 2))
            is_open = event.attributes.get("is_market_open")
            is_delayed = bool(event.raw_payload.get("is_delayed"))

            if age_seconds >= 900.0 or is_open is False:
                freshness = "stale"
            elif 60.0 <= age_seconds < 900.0 or is_delayed:
                freshness = "delayed"
            else:
                freshness = "live"

            price = float(event.price)
            high_val = (
                float(event.high)
                if event.high is not None and event.high > 0
                else price
            )
            low_val = (
                float(event.low) if event.low is not None and event.low > 0 else price
            )
            high = max(high_val, price)
            low = min(low_val, price)

            pct_change = event.attributes.get("percent_change")
            change_percent = float(pct_change) if pct_change is not None else 0.0

            name = str(event.attributes.get("name") or default_name)
            currency = str(event.attributes.get("currency") or "USD")

            live_markets.append(
                Market(
                    symbol=dash_sym,
                    name=name,
                    price=price,
                    change_percent=change_percent,
                    high=high,
                    low=low,
                    currency=currency,
                    is_mock=False,
                    data_source="twelve_data",
                    quote_timestamp=quote_ts,
                    quote_age_seconds=age_seconds,
                    freshness=freshness,
                    is_market_open=is_open if isinstance(is_open, bool) else None,
                )
            )

        return MarketListResponse(
            data_source="twelve_data",
            count=len(live_markets),
            markets=live_markets,
        )


def get_market_data_service() -> MarketDataService:
    """Factory for the market data service dependency."""
    api_key = get_twelve_data_api_key()
    if not api_key:
        return MarketDataService(client=None)

    try:
        client = TwelveDataClient(api_key=api_key)
        return MarketDataService(client=client)
    except TwelveDataConfigurationError:
        return MarketDataService(client=None)

