"""Source-specific adapters that implement the ingestion contracts."""

from app.connectors.alpha_vantage import (
    AlphaVantageNewsClient,
    AlphaVantageNewsConnector,
    alpha_vantage_news_connector,
)
from app.connectors.tradingview import TradingViewConnector, tradingview_connector
from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConnector,
    twelve_data_connector,
)

__all__ = [
    "AlphaVantageNewsClient",
    "AlphaVantageNewsConnector",
    "TradingViewConnector",
    "TwelveDataClient",
    "TwelveDataConnector",
    "alpha_vantage_news_connector",
    "tradingview_connector",
    "twelve_data_connector",
]
