"""Provider-neutral event contracts for the FRIDAY intelligence pipeline."""

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import AnyHttpUrl, Field, FiniteFloat, field_validator, model_validator

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
    POSITIONING_DATA = "positioning_data"
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


class OverallSentiment(APIModel):
    """Provider-neutral sentiment assessment for a news item."""

    score: FiniteFloat
    label: str = Field(min_length=1, max_length=80)

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class TopicRelevance(APIModel):
    """Provider-neutral relevance score for one news topic."""

    topic: str = Field(min_length=1, max_length=120)
    relevance_score: FiniteFloat

    @field_validator("topic", mode="before")
    @classmethod
    def normalize_topic(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class SymbolSentiment(APIModel):
    """Provider-neutral relevance and sentiment for one related symbol."""

    symbol: str = Field(min_length=1, max_length=40)
    relevance_score: FiniteFloat
    sentiment_score: FiniteFloat
    sentiment_label: str = Field(min_length=1, max_length=80)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("sentiment_label", mode="before")
    @classmethod
    def normalize_sentiment_label(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class NormalizedNewsResearchEvent(APIModel):
    """Provider-neutral news or research item awaiting future Core integration."""

    event_id: str = Field(min_length=1, max_length=120)
    source: SourceMetadata
    event_type: Literal[EventType.NEWS_ITEM] = EventType.NEWS_ITEM
    provider: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    url: AnyHttpUrl
    timestamp: datetime
    symbols: list[str] = Field(default_factory=list, max_length=50)
    topics: list[str] = Field(default_factory=list, max_length=50)
    authors: list[str] = Field(default_factory=list, max_length=50)
    publisher_domain: str | None = Field(default=None, max_length=253)
    category: str | None = Field(default=None, max_length=120)
    image_url: AnyHttpUrl | None = None
    overall_sentiment: OverallSentiment | None = None
    topic_relevance: list[TopicRelevance] = Field(default_factory=list, max_length=50)
    symbol_sentiment: list[SymbolSentiment] = Field(
        default_factory=list,
        max_length=50,
    )
    summary: str | None = Field(default=None, max_length=2_000)
    content: str | None = Field(default=None, max_length=10_000)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "provider",
        "title",
        "publisher_domain",
        "category",
        "summary",
        "content",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        normalized = (value.strip().upper() for value in values)
        return list(dict.fromkeys(value for value in normalized if value))

    @field_validator("topics")
    @classmethod
    def normalize_topics(cls, values: list[str]) -> list[str]:
        normalized = (value.strip() for value in values)
        return list(dict.fromkeys(value for value in normalized if value))

    @field_validator("authors")
    @classmethod
    def normalize_authors(cls, values: list[str]) -> list[str]:
        normalized = (value.strip() for value in values)
        return list(dict.fromkeys(value for value in normalized if value))

    @field_validator("topic_relevance")
    @classmethod
    def deduplicate_topic_relevance(
        cls,
        values: list[TopicRelevance],
    ) -> list[TopicRelevance]:
        unique_values: dict[str, TopicRelevance] = {}
        for value in values:
            unique_values.setdefault(value.topic, value)
        return list(unique_values.values())

    @field_validator("symbol_sentiment")
    @classmethod
    def deduplicate_symbol_sentiment(
        cls,
        values: list[SymbolSentiment],
    ) -> list[SymbolSentiment]:
        unique_values: dict[str, SymbolSentiment] = {}
        for value in values:
            unique_values.setdefault(value.symbol, value)
        return list(unique_values.values())

    @field_validator("timestamp", "received_at")
    @classmethod
    def normalize_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp values must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_news_contract(self) -> "NormalizedNewsResearchEvent":
        if self.source.domain is not DataDomain.FINANCIAL_NEWS:
            raise ValueError("news/research sources must use the financial_news domain")
        if self.summary is None and self.content is None:
            raise ValueError("a summary or content value is required")
        return self
