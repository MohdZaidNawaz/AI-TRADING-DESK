"""Provider-neutral event contracts for the FRIDAY intelligence pipeline."""

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import Field

from app.models.schemas import APIModel


class SourceTransport(str, Enum):
    WEBHOOK = "webhook"
    API = "api"
    FEED = "feed"
    MANUAL = "manual"


class DataDomain(str, Enum):
    MARKET_DATA = "market_data"
    FINANCIAL_NEWS = "financial_news"
    ECONOMIC_DATA = "economic_data"
    FUNDAMENTAL_DATA = "fundamental_data"
    SENTIMENT = "sentiment"
    ALTERNATIVE_DATA = "alternative_data"


class AssetClass(str, Enum):
    UNKNOWN = "unknown"
    EQUITY = "equity"
    FOREX = "forex"
    CRYPTO = "crypto"
    COMMODITY = "commodity"
    INDEX = "index"
    FIXED_INCOME = "fixed_income"


class EventType(str, Enum):
    MARKET_ALERT = "market_alert"
    MARKET_QUOTE = "market_quote"
    MARKET_CANDLE = "market_candle"
    NEWS_ITEM = "news_item"
    ECONOMIC_RELEASE = "economic_release"
    FUNDAMENTAL_UPDATE = "fundamental_update"
    SENTIMENT_SIGNAL = "sentiment_signal"
    OBSERVATION = "observation"


class SourceMetadata(APIModel):
    """Stable identity and classification for a provider connector."""

    source_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    transport: SourceTransport
    domain: DataDomain


class NormalizedDataEvent(APIModel):
    """Validated provider-neutral event that may be consumed by FRIDAY Core."""

    event_id: str
    source: SourceMetadata
    event_type: EventType
    event: str = Field(min_length=1, max_length=120)
    asset_class: AssetClass = AssetClass.UNKNOWN
    symbol: str | None = Field(default=None, min_length=1, max_length=40)
    exchange: str | None = Field(default=None, max_length=40)
    timeframe: str | None = Field(default=None, max_length=20)
    alert_name: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=4_000)
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    indicator_values: dict[str, float] = Field(default_factory=dict)
    timestamp: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any]
