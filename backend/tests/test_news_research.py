from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents import create_friday_core
from app.ingestion import NewsResearchIngestionService
from app.ingestion.contracts import NewsResearchConnector
from app.models.events import (
    DataDomain,
    EventType,
    NormalizedNewsResearchEvent,
    OverallSentiment,
    SourceMetadata,
    SourceTransport,
    SymbolSentiment,
    TopicRelevance,
)
from app.services.news_research import XauUsdNewsRelevanceService

NEWS_SOURCE = SourceMetadata(
    source_id="test_news_feed",
    display_name="Test News Feed",
    transport=SourceTransport.FEED,
    domain=DataDomain.FINANCIAL_NEWS,
)


def _news_event(
    *,
    event_id: str = "news-1",
    timestamp: datetime | None = None,
) -> NormalizedNewsResearchEvent:
    return NormalizedNewsResearchEvent(
        event_id=event_id,
        source=NEWS_SOURCE,
        provider="Example Research Wire",
        title="Central bank publishes its policy assessment",
        url="https://example.com/research/policy-assessment",
        timestamp=timestamp or datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        symbols=[" eur/usd ", "EUR/USD", "dxy"],
        topics=["Central Banks", "Monetary Policy", "Central Banks"],
        summary="A concise, provider-neutral research summary.",
    )


def test_normalized_news_research_event_preserves_required_fields() -> None:
    event = _news_event()

    assert event.event_type is EventType.NEWS_ITEM
    assert event.source == NEWS_SOURCE
    assert event.provider == "Example Research Wire"
    assert event.title == "Central bank publishes its policy assessment"
    assert str(event.url) == "https://example.com/research/policy-assessment"
    assert event.symbols == ["EUR/USD", "DXY"]
    assert event.topics == ["Central Banks", "Monetary Policy"]
    assert event.summary == "A concise, provider-neutral research summary."
    assert event.content is None


def test_news_event_accepts_provider_neutral_research_metadata_and_sentiment() -> None:
    event = NormalizedNewsResearchEvent(
        event_id="news-sentiment",
        source=NEWS_SOURCE,
        provider="Example Research Wire",
        title="Gold responds to the policy outlook",
        url="https://example.com/research/gold-policy-outlook",
        timestamp=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        symbols=["XAU/USD", "DXY"],
        topics=["Monetary Policy", "Commodities"],
        authors=[" Alice Analyst ", "Bob Reporter", "Alice Analyst"],
        publisher_domain=" example.com ",
        category=" Macro Research ",
        image_url="https://example.com/images/gold-policy-outlook.jpg",
        overall_sentiment={"score": 0.25, "label": " Somewhat Bullish "},
        topic_relevance=[
            {"topic": " Monetary Policy ", "relevance_score": 0.9},
            {"topic": "Commodities", "relevance_score": 0.8},
            {"topic": "Monetary Policy", "relevance_score": 0.7},
        ],
        symbol_sentiment=[
            {
                "symbol": " xau/usd ",
                "relevance_score": 0.95,
                "sentiment_score": 0.3,
                "sentiment_label": " Bullish ",
            },
            {
                "symbol": "XAU/USD",
                "relevance_score": 0.75,
                "sentiment_score": 0.2,
                "sentiment_label": "Somewhat Bullish",
            },
        ],
        summary="Gold moved as policy expectations changed.",
    )

    assert event.authors == ["Alice Analyst", "Bob Reporter"]
    assert event.publisher_domain == "example.com"
    assert event.category == "Macro Research"
    assert str(event.image_url) == (
        "https://example.com/images/gold-policy-outlook.jpg"
    )
    assert event.overall_sentiment == OverallSentiment(
        score=0.25,
        label="Somewhat Bullish",
    )
    assert event.topic_relevance == [
        TopicRelevance(topic="Monetary Policy", relevance_score=0.9),
        TopicRelevance(topic="Commodities", relevance_score=0.8),
    ]
    assert event.symbol_sentiment == [
        SymbolSentiment(
            symbol="XAU/USD",
            relevance_score=0.95,
            sentiment_score=0.3,
            sentiment_label="Bullish",
        )
    ]


def test_news_event_allows_missing_optional_research_metadata() -> None:
    event = _news_event()

    assert event.authors == []
    assert event.publisher_domain is None
    assert event.category is None
    assert event.image_url is None
    assert event.overall_sentiment is None
    assert event.topic_relevance == []
    assert event.symbol_sentiment == []


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("overall_sentiment", {"score": "not-a-number", "label": "Neutral"}),
        (
            "topic_relevance",
            [{"topic": "Macro", "relevance_score": "not-a-number"}],
        ),
        (
            "symbol_sentiment",
            [
                {
                    "symbol": "DXY",
                    "relevance_score": 0.8,
                    "sentiment_score": "not-a-number",
                    "sentiment_label": "Neutral",
                }
            ],
        ),
    ],
)
def test_news_event_rejects_malformed_sentiment_scores(
    field_name: str,
    value: object,
) -> None:
    payload = _news_event().model_dump(mode="json")
    payload[field_name] = value

    with pytest.raises(ValidationError):
        NormalizedNewsResearchEvent.model_validate(payload)


@pytest.mark.parametrize("non_finite_value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize(
    ("field_name", "value_factory"),
    [
        (
            "overall_sentiment",
            lambda value: {"score": value, "label": "Neutral"},
        ),
        (
            "topic_relevance",
            lambda value: [{"topic": "Macro", "relevance_score": value}],
        ),
        (
            "symbol_sentiment",
            lambda value: [
                {
                    "symbol": "DXY",
                    "relevance_score": value,
                    "sentiment_score": value,
                    "sentiment_label": "Neutral",
                }
            ],
        ),
    ],
)
def test_news_event_rejects_non_finite_sentiment_scores(
    field_name: str,
    value_factory: Any,
    non_finite_value: float,
) -> None:
    payload = _news_event().model_dump(mode="json")
    payload[field_name] = value_factory(non_finite_value)

    with pytest.raises(ValidationError, match="finite number"):
        NormalizedNewsResearchEvent.model_validate(payload)


def test_news_timestamp_is_normalized_to_utc() -> None:
    source_timezone = timezone(timedelta(hours=5, minutes=30))
    event = _news_event(
        timestamp=datetime(2026, 9, 29, 17, 30, tzinfo=source_timezone)
    )

    assert event.timestamp.isoformat() == "2026-09-29T12:00:00+00:00"


def test_news_event_requires_summary_or_content() -> None:
    with pytest.raises(ValidationError, match="summary or content"):
        NormalizedNewsResearchEvent(
            event_id="news-empty",
            source=NEWS_SOURCE,
            provider="Example Research Wire",
            title="Empty item",
            url="https://example.com/research/empty",
            timestamp=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        )


def test_news_event_rejects_a_non_news_source_domain() -> None:
    market_source = SourceMetadata(
        source_id="market_feed",
        display_name="Market Feed",
        transport=SourceTransport.API,
        domain=DataDomain.MARKET_DATA,
    )

    with pytest.raises(ValidationError, match="financial_news domain"):
        NormalizedNewsResearchEvent(
            event_id="news-wrong-domain",
            source=market_source,
            provider="Example Research Wire",
            title="Research item",
            url="https://example.com/research/item",
            timestamp=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
            content="Research content.",
        )


def test_news_event_contract_rejects_provider_specific_extra_fields() -> None:
    payload = _news_event().model_dump(mode="json")
    payload["provider_payload"] = {"provider_only_field": "not normalized"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalizedNewsResearchEvent.model_validate(payload)


def test_news_research_connector_contract_keeps_parsing_at_the_boundary() -> None:
    class ExampleConnector:
        source = NEWS_SOURCE

        def normalize(
            self,
            payload: Mapping[str, Any],
        ) -> NormalizedNewsResearchEvent:
            return NormalizedNewsResearchEvent(
                event_id=str(payload["id"]),
                source=self.source,
                provider=str(payload["publisher"]),
                title=str(payload["headline"]),
                url=str(payload["link"]),
                timestamp=payload["published_at"],
                topics=[str(payload["category"])],
                summary=str(payload["abstract"]),
            )

    connector = ExampleConnector()
    event = connector.normalize(
        {
            "id": "provider-1",
            "publisher": "Example Wire",
            "headline": "Policy outlook updated",
            "link": "https://example.com/news/policy-outlook",
            "published_at": "2026-09-29T12:00:00Z",
            "category": "Macro",
            "abstract": "The policy outlook was updated.",
        }
    )

    assert isinstance(connector, NewsResearchConnector)
    assert event.title == "Policy outlook updated"
    assert event.topics == ["Macro"]


def test_xau_usd_selector_accepts_a_non_alpha_normalized_news_source() -> None:
    event = NormalizedNewsResearchEvent(
        event_id="synthetic-fed-news",
        source=NEWS_SOURCE,
        provider="Synthetic Macro Wire",
        title="Federal Reserve updates its interest-rate outlook",
        url="https://example.com/news/fed-rate-outlook",
        timestamp=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        symbols=["USD"],
        topics=["Monetary Policy"],
        summary="The Fed discussed inflation and the future path of interest rates.",
    )

    selected = XauUsdNewsRelevanceService().select(
        [event],
        requested_count=1,
        sort="RELEVANCE",
    )

    assert selected == (event,)
    assert selected[0].source.source_id == "test_news_feed"


def test_news_research_ingestion_is_bounded_and_does_not_change_friday_core() -> None:
    service = NewsResearchIngestionService(max_events=2)
    core = create_friday_core()

    first = _news_event(event_id="news-1")
    second = _news_event(event_id="news-2")
    third = _news_event(event_id="news-3")
    service.ingest(first)
    service.ingest(second)
    accepted = service.ingest(third)

    assert accepted is third
    assert service.recent_events == (second, third)
    assert core.recent_market_events == ()
