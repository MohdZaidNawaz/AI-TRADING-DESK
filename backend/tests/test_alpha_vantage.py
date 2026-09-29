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
        assert request.url.params["limit"] == "100"
        assert secrets.compare_digest(request.url.params["apikey"], api_key)
        return httpx.Response(200, json={"items": "1", "feed": [_article_payload()]})

    records = _run(
        _mock_client(handler, api_key).fetch_news(
            tickers=["FOREX:USD", "CRYPTO:BTC"],
            topics="economy_monetary, financial_markets",
            time_from=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
            time_to="20260929T1200",
            sort="relevance",
            limit=100,
        )
    )

    assert len(records) == 1
    assert records[0]["title"] == _article_payload()["title"]


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
    assert event.symbols == ["FOREX:USD", "GLD"]
    assert event.overall_sentiment is not None
    assert event.overall_sentiment.score == 0.21
    assert event.overall_sentiment.label == "Somewhat-Bullish"
    assert event.symbol_sentiment[0].symbol == "FOREX:USD"
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


@pytest.mark.parametrize("invalid_limit", [True, 0, -1, 50.0, "50", 1_001])
def test_alpha_vantage_client_rejects_invalid_limit(invalid_limit: Any) -> None:
    client = _mock_client(lambda request: httpx.Response(200), "dummy-key")

    with pytest.raises(ValueError, match="limit must be"):
        _run(client.fetch_news(limit=invalid_limit))


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
