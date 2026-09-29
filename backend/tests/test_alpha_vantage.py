import asyncio
import logging
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app import config as config_module
from app.connectors.alpha_vantage import (
    AlphaVantageConfigurationError,
    AlphaVantageNewsClient,
    AlphaVantagePayloadError,
    AlphaVantageProviderError,
    AlphaVantageRateLimitError,
    alpha_vantage_news_connector,
)
from app.ingestion.contracts import NewsResearchConnector
from app.models.events import EventType, NormalizedNewsResearchEvent, SourceTransport
from app.services.alpha_vantage_news import AlphaVantageNewsResearchService


def _article_payload() -> dict[str, Any]:
    return {
        "title": "Dollar and gold react to the latest policy outlook",
        "url": "https://news.example.com/markets/gold?b=2&a=1#latest",
        "time_published": "20260929T120000",
        "authors": ["Alice Analyst", "Bob Reporter"],
        "summary": "Markets responded to updated monetary-policy expectations.",
        "banner_image": "https://news.example.com/images/gold.jpg",
        "source": "Example Research Wire",
        "category_within_source": "Macro",
        "source_domain": "news.example.com",
        "topics": [
            {"topic": "Economy - Monetary", "relevance_score": "0.95"},
            {"topic": "Financial Markets", "relevance_score": "0.80"},
        ],
        "overall_sentiment_score": "0.21",
        "overall_sentiment_label": "Somewhat-Bullish",
        "ticker_sentiment": [
            {
                "ticker": "FOREX:USD",
                "relevance_score": "0.91",
                "ticker_sentiment_score": "0.18",
                "ticker_sentiment_label": "Somewhat-Bullish",
            },
            {
                "ticker": "GLD",
                "relevance_score": "0.77",
                "ticker_sentiment_score": "0.12",
                "ticker_sentiment_label": "Neutral",
            },
        ],
    }


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _mock_client(handler: Any, api_key: str) -> AlphaVantageNewsClient:
    return AlphaVantageNewsClient(
        api_key,
        transport=httpx.MockTransport(handler),
    )


def _news_payload(
    index: int,
    *,
    title: str,
    summary: str,
    time_published: str | None = None,
    topics: list[dict[str, str]] | None = None,
    provider: str = "Example Research Wire",
    category: str = "Macro",
    symbol_sentiment: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload = _article_payload()
    payload.update(
        {
            "title": title,
            "url": f"https://news.example.com/articles/{index}",
            "time_published": time_published or f"20260929T12{index:02d}00",
            "summary": summary,
            "source": provider,
            "category_within_source": category,
            "topics": topics or [],
            "ticker_sentiment": symbol_sentiment or [],
        }
    )
    return payload


def _research_service(
    feed: list[Mapping[str, Any]],
    *,
    request_assertion: Any | None = None,
) -> AlphaVantageNewsResearchService:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["function"] == "NEWS_SENTIMENT"
        assert request.url.params["tickers"] == "FOREX:USD"
        assert request.url.params["limit"] == "50"
        if request_assertion is not None:
            request_assertion(request)
        return httpx.Response(200, json={"feed": feed})

    return AlphaVantageNewsResearchService(_mock_client(handler, api_key))


def _gold_feed(count: int = 50) -> list[dict[str, Any]]:
    return [
        _news_payload(
            index,
            title=f"Gold market update {index}",
            summary="Precious-metal markets remain active.",
        )
        for index in range(count)
    ]


@pytest.fixture(autouse=True)
def block_accidental_live_alpha_vantage_requests(monkeypatch: Any) -> None:
    async def fail_on_live_request(*args: Any, **kwargs: Any) -> httpx.Response:
        raise AssertionError("Alpha Vantage tests must not use the network.")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        fail_on_live_request,
    )


def test_alpha_vantage_client_supports_documented_news_query_parameters() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/query"
        assert request.url.params["function"] == "NEWS_SENTIMENT"
        assert request.url.params["tickers"] == "FOREX:USD,CRYPTO:BTC"
        assert request.url.params["topics"] == "economy_monetary,financial_markets"
        assert request.url.params["time_from"] == "20260928T1200"
        assert request.url.params["time_to"] == "20260929T1200"
        assert request.url.params["sort"] == "RELEVANCE"
        assert request.url.params["limit"] == "50"
        assert secrets.compare_digest(request.url.params["apikey"], api_key)
        return httpx.Response(200, json={"items": "1", "feed": [_article_payload()]})

    records = _run(
        _mock_client(handler, api_key).fetch_news(
            tickers=["FOREX:USD", "CRYPTO:BTC"],
            topics="economy_monetary, financial_markets",
            time_from=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
            time_to="20260929T1200",
            sort="relevance",
        )
    )

    assert len(records) == 1
    assert records[0]["title"] == _article_payload()["title"]


def test_xau_research_accepts_a_fifty_article_provider_page() -> None:
    events = _run(
        _research_service(_gold_feed()).research_xau_usd(requested_count=50)
    )

    assert len(events) == 50


def test_xau_research_returns_requested_count_after_provider_page() -> None:
    events = _run(
        _research_service(_gold_feed()).research_xau_usd(requested_count=3)
    )

    assert len(events) == 3


@pytest.mark.parametrize("requested_count", [True, 0, 51, 3.0, "3"])
def test_xau_research_rejects_invalid_requested_count(
    requested_count: Any,
) -> None:
    with pytest.raises(ValueError, match="requested_count must be"):
        _run(
            _research_service([]).research_xau_usd(
                requested_count=requested_count,
            )
        )


def test_xau_research_filters_irrelevant_usd_articles_below_gold_articles() -> None:
    feed = [
        _news_payload(
            0,
            title="Technology company reports quarterly revenue",
            summary="The company reported its results in US dollars.",
            time_published="20260929T130000",
        ),
        _news_payload(
            1,
            title="Gold strengthens against the dollar",
            summary="Bullion advanced during the session.",
            time_published="20260929T120000",
        ),
    ]

    events = _run(
        _research_service(feed).research_xau_usd(
            requested_count=2,
            sort="LATEST",
        )
    )

    assert [event.title for event in events] == [
        "Gold strengthens against the dollar"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        _news_payload(
            0,
            title="GoLd demand increases",
            summary="Metals remained active.",
        ),
        _news_payload(
            1,
            title="Metals market update",
            summary="xAu moved during the session.",
        ),
        _news_payload(
            2,
            title="Commodities market update",
            summary="Markets remained active.",
            topics=[{"topic": "BULLION", "relevance_score": "0.90"}],
        ),
        _news_payload(
            3,
            title="Commodities market update",
            summary="Markets remained active.",
            category="PRECIOUS METALS",
        ),
    ],
)
def test_xau_research_matches_gold_concepts_case_insensitively(
    payload: Mapping[str, Any],
) -> None:
    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1


def test_xau_research_keeps_fed_usd_context_without_gold_wording() -> None:
    payload = _news_payload(
        0,
        title="Federal Reserve decision lifts the US dollar",
        summary="The Fed kept interest rates unchanged after its policy meeting.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert [event.title for event in events] == [payload["title"]]


def test_xau_research_keeps_inflation_context() -> None:
    payload = _news_payload(
        0,
        title="Inflation cools faster than economists expected",
        summary="The price report changed the monetary-policy outlook.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1


def test_xau_research_keeps_real_yield_context() -> None:
    payload = _news_payload(
        0,
        title="Real yields climb after the latest economic data",
        summary="Real interest rates moved higher during the session.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1


def test_xau_research_keeps_treasury_yield_context() -> None:
    payload = _news_payload(
        0,
        title="Treasury yields rise across the curve",
        summary="US Treasuries weakened after the economic release.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1


def test_xau_research_keeps_geopolitical_risk_off_context() -> None:
    payload = _news_payload(
        0,
        title="Geopolitical tensions trigger a risk-off move",
        summary="The military escalation increased safe-haven demand.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1


@pytest.mark.parametrize(
    "title",
    [
        "Athlete wins gold medal in championship final",
        "Company describes its platform as the gold standard",
    ],
)
def test_xau_research_rejects_obvious_nonfinancial_gold_phrases(title: str) -> None:
    payload = _news_payload(
        0,
        title=title,
        summary="The story is unrelated to financial markets.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert events == ()


def test_xau_research_accepts_financial_gold_standard_coverage() -> None:
    payload = _news_payload(
        0,
        title="Economists revisit the historical gold standard",
        summary=(
            "The monetary system linked currency convertibility to reserve assets."
        ),
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert [event.title for event in events] == [payload["title"]]


@pytest.mark.parametrize(
    "phrase",
    [
        "precious metal",
        "precious metals",
        "precious-metal",
        "precious-metals",
    ],
)
def test_xau_research_accepts_precious_metal_variants(phrase: str) -> None:
    payload = _news_payload(
        0,
        title=f"{phrase} prices strengthen",
        summary="Commodity markets remained active.",
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        (
            "The animals were well fed throughout winter",
            "The agricultural report focused on livestock nutrition.",
        ),
        (
            "Retailers begin a commercial price war",
            "Competing stores announced discounts on consumer electronics.",
        ),
        (
            "Balloon inflation instructions updated for the festival",
            "Organizers published guidance for event volunteers.",
        ),
        (
            "Sports organization sanctions a player",
            "The disciplinary decision followed a rules violation.",
        ),
    ],
)
def test_xau_research_rejects_unrelated_context_phrases(
    title: str,
    summary: str,
) -> None:
    payload = _news_payload(0, title=title, summary=summary)

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert events == ()


@pytest.mark.parametrize(
    ("provider", "publisher_domain"),
    [
        ("Gold Research Wire", "news.example.com"),
        ("Example Research Wire", "gold-research.example.com"),
    ],
)
def test_xau_research_rejects_publisher_only_gold_match(
    provider: str,
    publisher_domain: str,
) -> None:
    payload = _news_payload(
        0,
        title="Technology company releases a new product",
        summary="The announcement covers product availability.",
        provider=provider,
    )
    payload["source_domain"] = publisher_domain

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert events == ()


def test_xau_research_uses_publisher_metadata_only_as_a_ranking_boost() -> None:
    ordinary_publisher = _news_payload(
        0,
        title="Gold market outlook",
        summary="Bullion demand remained steady.",
        time_published="20260929T120000",
    )
    specialist_publisher = _news_payload(
        1,
        title="Gold market outlook",
        summary="Bullion demand remained steady.",
        time_published="20260929T120000",
        provider="Gold Research Wire",
    )

    events = _run(
        _research_service(
            [ordinary_publisher, specialist_publisher]
        ).research_xau_usd(
            requested_count=2,
            sort="RELEVANCE",
        )
    )

    assert [event.provider for event in events] == [
        "Gold Research Wire",
        "Example Research Wire",
    ]


def test_xau_research_accepts_a_gold_related_ticker_signal() -> None:
    payload = _news_payload(
        0,
        title="Commodity fund positioning changes",
        summary="The fund recorded a change in institutional flows.",
        symbol_sentiment=[
            {
                "ticker": "GLD",
                "relevance_score": "0.96",
                "ticker_sentiment_score": "0.20",
                "ticker_sentiment_label": "Somewhat-Bullish",
            }
        ],
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1
    assert events[0].symbols == ["GLD"]


def test_xau_research_accepts_canonicalized_alpha_usd_signal() -> None:
    payload = _news_payload(
        0,
        title="Markets await the next session",
        summary="Cross-asset positioning changed during quiet trading.",
        symbol_sentiment=[
            {
                "ticker": "FOREX:USD",
                "relevance_score": "0.92",
                "ticker_sentiment_score": "0.10",
                "ticker_sentiment_label": "Neutral",
            }
        ],
    )

    events = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )

    assert len(events) == 1
    assert events[0].symbols == ["USD"]
    assert events[0].symbol_sentiment[0].symbol == "USD"


def test_xau_research_deduplicates_existing_event_ids() -> None:
    first = _news_payload(
        0,
        title="Gold market update",
        summary="Bullion advanced.",
    )
    second = dict(first)
    first["url"] = "https://news.example.com/gold-story#first"
    second["url"] = "https://news.example.com/gold-story#second"

    events = _run(
        _research_service([first, second]).research_xau_usd(requested_count=2)
    )

    assert len(events) == 1


def test_xau_research_returns_empty_when_no_articles_are_relevant() -> None:
    feed = [
        _news_payload(
            index,
            title=f"Consumer technology update {index}",
            summary="The company announced a new device and software release.",
        )
        for index in range(5)
    ]

    events = _run(
        _research_service(feed).research_xau_usd(requested_count=3)
    )

    assert events == ()


def test_xau_research_applies_recency_boundaries_locally_and_upstream() -> None:
    feed = [
        _news_payload(
            0,
            title="Older gold article",
            summary="Gold market context.",
            time_published="20260927T120000",
        ),
        _news_payload(
            1,
            title="Current gold article",
            summary="Gold market context.",
            time_published="20260929T120000",
        ),
    ]

    def assert_request(request: httpx.Request) -> None:
        assert request.url.params["time_from"] == "20260928T0000"
        assert request.url.params["time_to"] == "20260929T2359"

    events = _run(
        _research_service(feed, request_assertion=assert_request).research_xau_usd(
            requested_count=3,
            time_from="20260928T0000",
            time_to="20260929T2359",
        )
    )

    assert [event.title for event in events] == ["Current gold article"]


def test_xau_research_supports_relevance_and_latest_local_sorting() -> None:
    older_relevant = _news_payload(
        0,
        title="Gold XAU bullion and precious metals outlook",
        summary="A detailed commodities assessment.",
        time_published="20260928T120000",
    )
    latest_relevant = _news_payload(
        1,
        title="Latest commodities update",
        summary="Gold moved during the latest session.",
        time_published="20260929T120000",
    )
    feed = [older_relevant, latest_relevant]

    def assert_relevance_request(request: httpx.Request) -> None:
        assert request.url.params["sort"] == "RELEVANCE"

    def assert_latest_request(request: httpx.Request) -> None:
        assert request.url.params["sort"] == "LATEST"

    relevance_events = _run(
        _research_service(
            feed,
            request_assertion=assert_relevance_request,
        ).research_xau_usd(
            requested_count=2,
            sort="RELEVANCE",
        )
    )
    latest_events = _run(
        _research_service(
            feed,
            request_assertion=assert_latest_request,
        ).research_xau_usd(
            requested_count=2,
            sort="LATEST",
        )
    )

    assert [event.title for event in relevance_events] == [
        "Gold XAU bullion and precious metals outlook",
        "Latest commodities update",
    ]
    assert [event.title for event in latest_events] == [
        "Latest commodities update",
        "Gold XAU bullion and precious metals outlook",
    ]


def test_xau_research_ranks_direct_gold_above_contextual_macro_news() -> None:
    direct = _news_payload(
        0,
        title="Gold demand strengthens",
        summary="Bullion buying increased during the session.",
        time_published="20260929T110000",
    )
    contextual = _news_payload(
        1,
        title="Federal Reserve updates its policy outlook",
        summary="The Fed discussed interest rates and inflation.",
        time_published="20260929T130000",
    )

    events = _run(
        _research_service([contextual, direct]).research_xau_usd(
            requested_count=2,
            sort="RELEVANCE",
        )
    )

    assert [event.title for event in events] == [direct["title"], contextual["title"]]


def test_xau_research_rejects_invalid_time_range_before_provider_call() -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={"feed": []})

    service = AlphaVantageNewsResearchService(
        _mock_client(handler, secrets.token_urlsafe(32))
    )

    with pytest.raises(ValueError, match="time_from must not be after time_to"):
        _run(
            service.research_xau_usd(
                time_from="20260930T0000",
                time_to="20260929T0000",
            )
        )

    assert provider_calls == 0


def test_xau_research_rejects_inverted_same_minute_datetimes_before_call() -> None:
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={"feed": []})

    service = AlphaVantageNewsResearchService(
        _mock_client(handler, secrets.token_urlsafe(32))
    )

    with pytest.raises(ValueError, match="time_from must not be after time_to"):
        _run(
            service.research_xau_usd(
                time_from=datetime(2026, 9, 29, 12, 0, 50, tzinfo=UTC),
                time_to=datetime(2026, 9, 29, 12, 0, 10, tzinfo=UTC),
            )
        )

    assert provider_calls == 0


def test_xau_research_applies_exact_second_bounds_locally() -> None:
    feed = [
        _news_payload(
            index,
            title=f"Gold boundary article {second}",
            summary="Bullion market context.",
            time_published=f"20260929T1200{second:02d}",
        )
        for index, second in enumerate((9, 10, 30, 50, 51))
    ]

    def assert_request(request: httpx.Request) -> None:
        assert request.url.params["time_from"] == "20260929T1200"
        assert request.url.params["time_to"] == "20260929T1200"

    events = _run(
        _research_service(feed, request_assertion=assert_request).research_xau_usd(
            requested_count=5,
            time_from=datetime(2026, 9, 29, 12, 0, 10, tzinfo=UTC),
            time_to=datetime(2026, 9, 29, 12, 0, 50, tzinfo=UTC),
            sort="LATEST",
        )
    )

    assert [event.timestamp.second for event in events] == [50, 30, 10]


def test_xau_research_latest_prefers_newest_duplicate() -> None:
    older = _news_payload(
        0,
        title="Gold XAU bullion outlook",
        summary="Precious metals remained active.",
        time_published="20260928T120000",
    )
    newer = _news_payload(
        1,
        title="Federal Reserve policy update",
        summary="Interest rates remained unchanged.",
        time_published="20260929T120000",
    )
    older["url"] = "https://news.example.com/shared-story#older"
    newer["url"] = "https://news.example.com/shared-story#newer"

    events = _run(
        _research_service([older, newer]).research_xau_usd(
            requested_count=2,
            sort="LATEST",
        )
    )

    assert [event.title for event in events] == [newer["title"]]


def test_xau_research_relevance_prefers_stronger_duplicate() -> None:
    stronger = _news_payload(
        0,
        title="Gold XAU bullion and precious metals outlook",
        summary="Bullion demand strengthened.",
        time_published="20260928T120000",
    )
    newer_context = _news_payload(
        1,
        title="Federal Reserve policy update",
        summary="Interest rates remained unchanged.",
        time_published="20260929T120000",
    )
    stronger["url"] = "https://news.example.com/shared-story#stronger"
    newer_context["url"] = "https://news.example.com/shared-story#newer"

    events = _run(
        _research_service([newer_context, stronger]).research_xau_usd(
            requested_count=2,
            sort="RELEVANCE",
        )
    )

    assert [event.title for event in events] == [stronger["title"]]


@pytest.mark.parametrize("sort", ["RELEVANCE", "LATEST"])
def test_xau_research_tie_order_is_independent_of_provider_order(
    sort: str,
) -> None:
    first = _news_payload(
        0,
        title="Gold market outlook alpha",
        summary="Bullion demand remained steady.",
        time_published="20260929T120000",
    )
    second = _news_payload(
        1,
        title="Gold market outlook bravo",
        summary="Bullion demand remained steady.",
        time_published="20260929T120000",
    )

    forward = _run(
        _research_service([first, second]).research_xau_usd(
            requested_count=2,
            sort=sort,
        )
    )
    reversed_events = _run(
        _research_service([second, first]).research_xau_usd(
            requested_count=2,
            sort=sort,
        )
    )

    assert [event.event_id for event in forward] == [
        event.event_id for event in reversed_events
    ]


def test_xau_research_result_count_is_applied_after_relevance_filtering() -> None:
    feed = [
        _news_payload(
            index,
            title=(
                f"Gold update {index}"
                if index < 2
                else f"Technology update {index}"
            ),
            summary="General reporting context.",
        )
        for index in range(50)
    ]

    events = _run(
        _research_service(feed).research_xau_usd(requested_count=3)
    )

    assert len(events) == 2
    assert all("Gold" in event.title for event in events)


def test_xau_research_preserves_normalized_sentiment_and_relevance() -> None:
    payload = _article_payload()

    event = _run(
        _research_service([payload]).research_xau_usd(requested_count=1)
    )[0]

    assert event.overall_sentiment is not None
    assert event.overall_sentiment.score == 0.21
    assert event.overall_sentiment.label == "Somewhat-Bullish"
    assert event.topic_relevance[0].relevance_score == 0.95
    assert event.symbol_sentiment[0].symbol == "USD"
    assert event.symbol_sentiment[0].relevance_score == 0.91
    assert event.symbol_sentiment[0].sentiment_score == 0.18
    assert event.content is None


def test_alpha_vantage_api_key_never_reaches_httpx_or_debug_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        assert secrets.compare_digest(request.url.params["apikey"], api_key)
        return httpx.Response(200, json={"feed": [_article_payload()]})

    caplog.set_level(logging.DEBUG)
    records = _run(_mock_client(handler, api_key).fetch_news())

    assert records
    assert "HTTP Request:" in caplog.text
    assert api_key not in caplog.text
    assert "apikey=" not in caplog.text


@pytest.mark.parametrize(
    "base_url",
    [
        "http://www.alphavantage.co",
        "https://alpha-vantage.example.com",
        "https://www.alphavantage.co.example.com",
        "https://user:password@www.alphavantage.co",
        "https://www.alphavantage.co/query",
    ],
)
def test_alpha_vantage_client_rejects_untrusted_base_urls(base_url: str) -> None:
    with pytest.raises(AlphaVantageConfigurationError, match="not permitted"):
        AlphaVantageNewsClient(secrets.token_urlsafe(32), base_url=base_url)


def test_alpha_vantage_connector_normalizes_a_news_article() -> None:
    event = alpha_vantage_news_connector.normalize(_article_payload())

    assert isinstance(event, NormalizedNewsResearchEvent)
    assert isinstance(alpha_vantage_news_connector, NewsResearchConnector)
    assert event.source.source_id == "alpha_vantage_news"
    assert event.source.transport is SourceTransport.API
    assert event.event_type is EventType.NEWS_ITEM
    assert event.provider == "Example Research Wire"
    assert event.title == "Dollar and gold react to the latest policy outlook"
    assert str(event.url) == (
        "https://news.example.com/markets/gold?b=2&a=1#latest"
    )
    assert event.timestamp.isoformat() == "2026-09-29T12:00:00+00:00"
    assert event.authors == ["Alice Analyst", "Bob Reporter"]
    assert event.publisher_domain == "news.example.com"
    assert event.category == "Macro"
    assert str(event.image_url) == "https://news.example.com/images/gold.jpg"
    assert event.summary == (
        "Markets responded to updated monetary-policy expectations."
    )
    assert event.content is None


def test_alpha_vantage_connector_normalizes_topics_and_symbol_sentiment() -> None:
    event = alpha_vantage_news_connector.normalize(_article_payload())

    assert event.topics == ["Economy - Monetary", "Financial Markets"]
    assert [item.relevance_score for item in event.topic_relevance] == [0.95, 0.8]
    assert event.symbols == ["USD", "GLD"]
    assert event.overall_sentiment is not None
    assert event.overall_sentiment.score == 0.21
    assert event.overall_sentiment.label == "Somewhat-Bullish"
    assert event.symbol_sentiment[0].symbol == "USD"
    assert event.symbol_sentiment[0].relevance_score == 0.91
    assert event.symbol_sentiment[0].sentiment_score == 0.18
    assert event.symbol_sentiment[0].sentiment_label == "Somewhat-Bullish"


def test_alpha_vantage_connector_allows_missing_optional_fields() -> None:
    payload = _article_payload()
    for key in (
        "authors",
        "banner_image",
        "category_within_source",
        "source_domain",
        "topics",
        "overall_sentiment_score",
        "overall_sentiment_label",
        "ticker_sentiment",
    ):
        payload.pop(key)

    event = alpha_vantage_news_connector.normalize(payload)

    assert event.authors == []
    assert event.image_url is None
    assert event.category is None
    assert event.publisher_domain is None
    assert event.topics == []
    assert event.topic_relevance == []
    assert event.overall_sentiment is None
    assert event.symbols == []
    assert event.symbol_sentiment == []
    assert event.content is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("url", "not-a-url"),
        ("time_published", "2026-09-29"),
        ("summary", None),
        (
            "topics",
            [{"topic": "Macro", "relevance_score": "not-a-number"}],
        ),
        (
            "ticker_sentiment",
            [
                {
                    "ticker": "FOREX:USD",
                    "relevance_score": "nan",
                    "ticker_sentiment_score": "inf",
                    "ticker_sentiment_label": "Neutral",
                }
            ],
        ),
    ],
)
def test_alpha_vantage_connector_rejects_malformed_provider_records(
    field_name: str,
    value: object,
) -> None:
    payload = _article_payload()
    payload[field_name] = value

    with pytest.raises(AlphaVantagePayloadError):
        alpha_vantage_news_connector.normalize(payload)


def test_alpha_vantage_preserves_complex_publisher_urls() -> None:
    publisher_url = (
        "https://news.example.com/article?tag=first&tag=second"
        "&encoded=a%2Bb%20c&X-Amz-Signature=abc%2F123%3D#section-2"
    )
    payload = _article_payload()
    payload["url"] = publisher_url

    event = alpha_vantage_news_connector.normalize(payload)

    assert str(event.url) == publisher_url


def test_alpha_vantage_event_ids_are_deterministic_without_rewriting_queries() -> None:
    first_payload = _article_payload()
    second_payload = _article_payload()
    second_payload["url"] = "https://news.example.com/markets/gold?b=2&a=1#other"

    first = alpha_vantage_news_connector.normalize(first_payload)
    second = alpha_vantage_news_connector.normalize(second_payload)

    assert first.event_id == second.event_id
    assert first.event_id.startswith("alpha-vantage-news-")
    assert str(first.url).endswith("#latest")
    assert str(second.url).endswith("#other")


def test_alpha_vantage_event_ids_preserve_query_order_and_duplicates() -> None:
    first_payload = _article_payload()
    second_payload = _article_payload()
    first_payload["url"] = "https://news.example.com/item?tag=one&tag=two"
    second_payload["url"] = "https://news.example.com/item?tag=two&tag=one"

    first = alpha_vantage_news_connector.normalize(first_payload)
    second = alpha_vantage_news_connector.normalize(second_payload)

    assert first.event_id != second.event_id


@pytest.mark.parametrize(
    ("response_payload", "expected_error", "expected_diagnostic"),
    [
        (
            {"Error Message": "Invalid API call."},
            AlphaVantageProviderError,
            "Invalid API call.",
        ),
        (
            {"message": "The requested news filters are invalid."},
            AlphaVantageProviderError,
            "The requested news filters are invalid.",
        ),
        (
            {"Note": "The standard API rate limit is 25 requests per day."},
            AlphaVantageRateLimitError,
            "The standard API rate limit is 25 requests per day.",
        ),
        (
            {"Information": "API call frequency has been exceeded."},
            AlphaVantageRateLimitError,
            "API call frequency has been exceeded.",
        ),
    ],
)
def test_alpha_vantage_client_handles_documented_error_responses(
    response_payload: Mapping[str, Any],
    expected_error: type[AlphaVantageProviderError],
    expected_diagnostic: str,
) -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    with pytest.raises(expected_error) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    assert expected_diagnostic in str(error.value)
    assert api_key not in str(error.value)


def test_alpha_vantage_client_handles_http_errors_without_leaking_the_key() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"failed for {api_key}")

    with pytest.raises(AlphaVantageProviderError) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    assert api_key not in str(error.value)


def test_alpha_vantage_client_handles_http_429() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=f"limited {api_key}")

    with pytest.raises(AlphaVantageRateLimitError) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    assert api_key not in str(error.value)


def test_alpha_vantage_client_handles_request_errors_without_secret_leakage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"failed for {api_key}", request=request)

    caplog.set_level(logging.DEBUG)
    with pytest.raises(AlphaVantageProviderError) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    assert api_key not in str(error.value)
    assert api_key not in caplog.text


def test_alpha_vantage_client_handles_timeouts_without_secret_leakage() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out for {api_key}", request=request)

    with pytest.raises(AlphaVantageProviderError) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    assert api_key not in str(error.value)


def test_alpha_vantage_client_rejects_malformed_json() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(AlphaVantagePayloadError, match="malformed news data"):
        _run(_mock_client(handler, api_key).fetch_news())


@pytest.mark.parametrize(
    "response_payload",
    [
        {},
        {"feed": None},
        {"feed": {}},
        {"feed": "not-a-list"},
    ],
)
def test_alpha_vantage_client_rejects_missing_or_non_list_feed(
    response_payload: Mapping[str, Any],
) -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    with pytest.raises(AlphaVantagePayloadError, match="malformed news data"):
        _run(_mock_client(handler, api_key).fetch_news())


def test_alpha_vantage_client_rejects_malformed_feed_records() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"feed": [_article_payload(), None]})

    with pytest.raises(AlphaVantagePayloadError, match="malformed news data"):
        _run(_mock_client(handler, api_key).fetch_news())


def test_alpha_vantage_client_allows_an_empty_feed() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"feed": []})

    assert _run(_mock_client(handler, api_key).fetch_news()) == ()


@pytest.mark.parametrize("invalid_sort", ["", "oldest", "random"])
def test_alpha_vantage_client_rejects_invalid_sort(invalid_sort: str) -> None:
    client = _mock_client(lambda request: httpx.Response(200), "dummy-key")

    with pytest.raises(ValueError, match="sort must be"):
        _run(client.fetch_news(sort=invalid_sort))


def test_alpha_vantage_client_rejects_naive_query_datetimes() -> None:
    client = _mock_client(lambda request: httpx.Response(200), "dummy-key")

    with pytest.raises(ValueError, match="must include a timezone"):
        _run(client.fetch_news(time_from=datetime(2026, 9, 29, 12, 0)))


def test_alpha_vantage_client_converts_query_datetimes_to_utc() -> None:
    api_key = secrets.token_urlsafe(32)
    india_standard_time = timezone(timedelta(hours=5, minutes=30))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["time_from"] == "20260929T0630"
        return httpx.Response(200, json={"feed": []})

    records = _run(
        _mock_client(handler, api_key).fetch_news(
            time_from=datetime(2026, 9, 29, 12, 0, tzinfo=india_standard_time)
        )
    )

    assert records == ()


def test_alpha_vantage_client_redacts_an_echoed_api_key() -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        article = _article_payload()
        article["apikey"] = api_key
        article["summary"] = f"Provider accidentally echoed {api_key}."
        return httpx.Response(200, json={"feed": [article]})

    records = _run(_mock_client(handler, api_key).fetch_news())
    serialized_records = repr(records)
    normalized_event = alpha_vantage_news_connector.normalize(records[0])

    assert api_key not in serialized_records
    assert "apikey" not in records[0]
    assert "[REDACTED]" in str(records[0]["summary"])
    assert api_key not in normalized_event.model_dump_json()


def test_alpha_vantage_provider_error_payload_cannot_expose_the_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api_key = secrets.token_urlsafe(32)
    provider_url = (
        "https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
        f"&apikey={api_key}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": (
                    f"Invalid credential: {api_key}. Request URL: {provider_url}"
                ),
                "raw_provider_detail": "raw-provider-marker",
            },
        )

    caplog.set_level(logging.DEBUG)
    with pytest.raises(AlphaVantageProviderError) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    diagnostic = str(error.value)
    assert "Provider diagnostic:" in diagnostic
    assert "[REDACTED]" in diagnostic
    assert "[REDACTED_URL]" in diagnostic
    assert "alphavantage.co" not in diagnostic
    assert "raw-provider-marker" not in diagnostic
    assert api_key not in diagnostic
    assert api_key not in caplog.text
    assert "raw-provider-marker" not in caplog.text


@pytest.mark.parametrize("unsafe_value", [None, "", {"detail": "not text"}])
def test_alpha_vantage_provider_error_uses_generic_fallback_without_safe_text(
    unsafe_value: Any,
) -> None:
    api_key = secrets.token_urlsafe(32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": unsafe_value})

    with pytest.raises(AlphaVantageProviderError) as error:
        _run(_mock_client(handler, api_key).fetch_news())

    assert str(error.value) == "Alpha Vantage news request failed."


def test_alpha_vantage_connector_rejects_malformed_overall_sentiment() -> None:
    payload = _article_payload()
    payload["overall_sentiment_score"] = "not-a-finite-number"

    with pytest.raises(AlphaVantagePayloadError, match="malformed news article"):
        alpha_vantage_news_connector.normalize(payload)


@pytest.mark.parametrize("configured_value", [None, "", "   "])
def test_alpha_vantage_client_rejects_missing_or_blank_api_key(
    monkeypatch: Any,
    configured_value: str | None,
) -> None:
    if configured_value is None:
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", configured_value)

    with pytest.raises(AlphaVantageConfigurationError, match="not configured"):
        AlphaVantageNewsClient.from_environment(load_env_file=False)


def test_alpha_vantage_client_reads_only_its_named_environment_variable(
    monkeypatch: Any,
) -> None:
    api_key = secrets.token_urlsafe(32)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", api_key)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", secrets.token_urlsafe(32))

    client = AlphaVantageNewsClient.from_environment(load_env_file=False)

    assert isinstance(client, AlphaVantageNewsClient)
    assert api_key not in repr(client)


def test_alpha_vantage_client_uses_env_file_as_a_fallback(
    monkeypatch: Any,
) -> None:
    api_key = secrets.token_urlsafe(32)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.setattr(
        config_module,
        "dotenv_values",
        lambda path: {"ALPHA_VANTAGE_API_KEY": api_key},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert secrets.compare_digest(request.url.params["apikey"], api_key)
        return httpx.Response(200, json={"feed": []})

    client = AlphaVantageNewsClient.from_environment(
        transport=httpx.MockTransport(handler),
    )

    assert _run(client.fetch_news()) == ()
    assert api_key not in repr(client)


def test_alpha_vantage_process_environment_takes_precedence_over_env_file(
    monkeypatch: Any,
) -> None:
    process_api_key = secrets.token_urlsafe(32)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", process_api_key)
    monkeypatch.setattr(
        config_module,
        "dotenv_values",
        lambda path: pytest.fail(".env must not be read when process env is set"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert secrets.compare_digest(
            request.url.params["apikey"],
            process_api_key,
        )
        return httpx.Response(200, json={"feed": []})

    client = AlphaVantageNewsClient.from_environment(
        transport=httpx.MockTransport(handler),
    )

    assert _run(client.fetch_news()) == ()
