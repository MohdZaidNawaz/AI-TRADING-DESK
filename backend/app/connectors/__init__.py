"""Source-specific adapters that implement the ingestion contracts."""

from app.connectors.alpha_vantage import (
    AlphaVantageNewsClient,
    AlphaVantageNewsConnector,
    alpha_vantage_news_connector,
)
from app.connectors.gdelt import (
    GdeltNewsClient,
    GdeltNewsConnector,
    gdelt_news_connector,
)
from app.connectors.tradingview import TradingViewConnector, tradingview_connector
from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConnector,
    twelve_data_connector,
)
from app.connectors.bea import BeaMacroProvider
from app.connectors.bls import BlsMacroProvider
from app.connectors.federal_reserve import FederalReserveMacroProvider
from app.connectors.fred import FredMacroProvider
from app.connectors.trading_economics import TradingEconomicsMacroProvider
from app.connectors.us_treasury import TreasuryMacroProvider

__all__ = [
    "AlphaVantageNewsClient",
    "AlphaVantageNewsConnector",
    "GdeltNewsClient",
    "GdeltNewsConnector",
    "TradingViewConnector",
    "TwelveDataClient",
    "TwelveDataConnector",
    "BeaMacroProvider",
    "BlsMacroProvider",
    "FederalReserveMacroProvider",
    "FredMacroProvider",
    "TradingEconomicsMacroProvider",
    "TreasuryMacroProvider",
    "alpha_vantage_news_connector",
    "gdelt_news_connector",
    "tradingview_connector",
    "twelve_data_connector",
]
