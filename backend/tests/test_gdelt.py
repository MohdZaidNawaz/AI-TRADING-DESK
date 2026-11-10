from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import ssl
from typing import Any

import httpx
import pytest

from app.agents import create_friday_core
from app.connectors.gdelt import (
    GDELT_DOC_PATH,
    GDELT_MAX_RECORDS,
    GdeltErrorCategory,
    GdeltInvalidRequestError,
    GdeltNetworkError,
    GdeltNewsClient,
    GdeltNewsConnector,
    GdeltPayloadError,
    GdeltRateLimitError,
    GdeltServerError,
    GdeltTimeoutError,
)
from app.models.news_intelligence import (
    EvidenceFreshness,
    NewsCacheStatus,
    NewsCooldownStatus,
    NewsEvidenceOrigin,
    NewsRetrievalStatus,
)
from app.models.schemas import AnalyzeRequest
from app.services.news_intelligence import (
    GDELT_SEARCHES,
    GdeltNewsIntelligenceRetrievalService,
    get_news_intelligence_retrieval_service,
)

NOW = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


def _run(awaitable):
    return asyncio.run(awaitable)


def _article(
    index: int = 1,
    *,
    title: str = "Gold rises as Federal Reserve rate outlook changes",
    url: str | None = None,
    seen_at: datetime = NOW - timedelta(hours=1),
) -> dict[str, Any]:
    return {
        "url": url or f"https://example{index}.test/news/gold-{index}",
        "title": title,
        "seendate": seen_at.strftime("%Y%m%dT%H%M%SZ"),
        "domain": f"example{index}.test",
        "language": "English",
        "sourcecountry": "United States",
    }


def _client(handler, **kwargs: Any) -> GdeltNewsClient:
    return GdeltNewsClient(transport=httpx.MockTransport(handler), **kwargs)


def test_gdelt_client_uses_bounded_credential_free_article_list_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == GDELT_DOC_PATH
        params = request.url.params
        assert params["query"] == "gold OR XAU"
        assert params["mode"] == "artlist"
        assert params["format"] == "json"
        assert params["timespan"] == "1week"
        assert params["maxrecords"] == str(GDELT_MAX_RECORDS)
        assert params["sort"] == "datedesc"
        assert "apikey" not in params
        assert "token" not in params
        return httpx.Response(200, json={"articles": [_article()]})

    records = _run(_client(handler).fetch_articles(query="gold OR XAU"))

    assert len(records) == 1


def test_gdelt_client_preserves_the_configured_gold_query() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == GDELT_DOC_PATH
        assert request.url.params["query"] == GDELT_SEARCHES[0][0]
        return httpx.Response(200, json={"articles": []})

    records = _run(_client(handler).fetch_articles(query=GDELT_SEARCHES[0][0]))

    assert records == ()
    assert calls == 1


def test_gdelt_client_uses_explicit_verified_transport_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class CapturingAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> CapturingAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self, path: str, *, params: Mapping[str, str]
        ) -> httpx.Response:
            request = httpx.Request(
                "GET",
                f"https://api.gdeltproject.org{path}",
                params=params,
            )
            return httpx.Response(200, json={"articles": []}, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)

    records = _run(GdeltNewsClient().fetch_articles(query="gold OR XAU"))

    assert records == ()
    assert captured["trust_env"] is False
    timeout = httpx.Timeout(captured["timeout"])
    assert timeout.connect == 10.0
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 10.0
    assert captured["transport"] is None
    ssl_context = captured["verify"]
    assert isinstance(ssl_context, ssl.SSLContext)
    assert ssl_context.verify_mode is ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


def test_gdelt_client_accepts_empty_results() -> None:
    records = _run(
        _client(lambda request: httpx.Response(200, json={"articles": []}))
        .fetch_articles(query="gold")
    )
    assert records == ()


def test_gdelt_connector_preserves_seen_metadata_without_inventing_publication() -> None:
    event = GdeltNewsConnector().normalize(
        _article(),
        search_query="gold OR XAU",
        relevance_category="Gold and precious metals",
    )

    assert event.title == "Gold rises as Federal Reserve rate outlook changes"
    assert str(event.url) == "https://example1.test/news/gold-1"
    assert event.timestamp == NOW - timedelta(hours=1)
    assert event.seen_at == NOW - timedelta(hours=1)
    assert event.published_at is None
    assert event.summary is None
    assert event.overall_sentiment is None
    assert event.publisher_domain == "example1.test"
    assert event.language == "English"
    assert event.source_country == "United States"
    assert event.search_query == "gold OR XAU"
    assert event.search_category == "Gold and precious metals"


@pytest.mark.parametrize("missing", ["title", "url", "seendate"])
def test_gdelt_connector_rejects_missing_required_metadata(missing: str) -> None:
    payload = _article()
    payload.pop(missing)
    with pytest.raises(GdeltPayloadError):
        GdeltNewsConnector().normalize(
            payload,
            search_query="gold",
            relevance_category="Gold and precious metals",
        )


def test_gdelt_connector_rejects_invalid_url() -> None:
    with pytest.raises(GdeltPayloadError, match="URL is invalid"):
        GdeltNewsConnector().normalize(
            _article(url="javascript:alert(1)"),
            search_query="gold",
            relevance_category="Gold and precious metals",
        )


def test_gdelt_client_rejects_malformed_json_without_retaining_body() -> None:
    with pytest.raises(GdeltPayloadError) as error:
        _run(
            _client(
                lambda request: httpx.Response(
                    200, content=b"not-json secret provider body"
                )
            ).fetch_articles(query="gold")
        )
    assert error.value.category is GdeltErrorCategory.MALFORMED_RESPONSE
    assert error.value.provider_reason_code == "INVALID_JSON"
    assert "secret provider body" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "error_type", "category", "reason"),
    [
        (400, GdeltInvalidRequestError, GdeltErrorCategory.INVALID_REQUEST, "HTTP_INVALID_REQUEST"),
        (503, GdeltServerError, GdeltErrorCategory.SERVER_ERROR, "HTTP_SERVER_ERROR"),
    ],
)
def test_gdelt_client_maps_http_failures(
    status: int,
    error_type: type[Exception],
    category: GdeltErrorCategory,
    reason: str,
) -> None:
    with pytest.raises(error_type) as error:
        _run(
            _client(
                lambda request: httpx.Response(
                    status, text="unretained provider body"
                )
            ).fetch_articles(query="gold")
        )
    assert error.value.category is category
    assert error.value.http_status_code == status
    assert error.value.provider_reason_code == reason
    assert "unretained provider body" not in str(error.value)


def test_gdelt_client_maps_rate_limit_without_retry_after() -> None:
    with pytest.raises(GdeltRateLimitError) as error:
        _run(
            _client(lambda request: httpx.Response(429)).fetch_articles(query="gold")
        )

    assert error.value.category is GdeltErrorCategory.RATE_LIMIT
    assert error.value.http_status_code == 429
    assert error.value.http_status_class == "4xx"
    assert error.value.provider_reason_code == "HTTP_RATE_LIMIT"
    assert error.value.failure_stage == "provider_response"
    assert error.value.request_sent is True
    assert error.value.retry_after_seconds is None


def test_gdelt_client_parses_numeric_retry_after_without_retrying() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(GdeltRateLimitError) as error:
        _run(_client(handler, now=lambda: NOW).fetch_articles(query="gold"))

    assert calls == 1
    assert error.value.retry_after_seconds == 120


def test_gdelt_client_parses_http_date_retry_after_without_retrying() -> None:
    retry_at = NOW + timedelta(seconds=90)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
        )

    with pytest.raises(GdeltRateLimitError) as error:
        _run(_client(handler, now=lambda: NOW).fetch_articles(query="gold"))

    assert calls == 1
    assert error.value.retry_after_seconds == 90


@pytest.mark.parametrize(
    "retry_after",
    ["", "-1", "1.5", "not-a-date", "9" * 129],
)
def test_gdelt_client_discards_invalid_retry_after(retry_after: str) -> None:
    with pytest.raises(GdeltRateLimitError) as error:
        _run(
            _client(
                lambda request: httpx.Response(
                    429,
                    headers={"Retry-After": retry_after},
                ),
                now=lambda: NOW,
            ).fetch_articles(query="gold")
        )

    assert error.value.retry_after_seconds is None
    assert str(error.value) == "GDELT request limit reached."


@pytest.mark.parametrize(
    ("error_type", "reason", "request_sent"),
    [
        (httpx.ConnectTimeout, "CONNECT_TIMEOUT", False),
        (httpx.ReadTimeout, "READ_TIMEOUT", True),
        (httpx.WriteTimeout, "WRITE_TIMEOUT", None),
        (httpx.PoolTimeout, "POOL_TIMEOUT", False),
    ],
)
def test_gdelt_client_maps_timeout_stage_without_retry(
    error_type: type[httpx.TimeoutException],
    reason: str,
    request_sent: bool | None,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error_type("private transport detail", request=request)

    with pytest.raises(GdeltTimeoutError) as error:
        _run(_client(handler).fetch_articles(query="gold"))
    assert calls == 1
    assert error.value.category is GdeltErrorCategory.TIMEOUT
    assert error.value.provider_reason_code == reason
    assert error.value.request_sent is request_sent
    assert "private transport detail" not in str(error.value)


def test_gdelt_client_maps_network_failure_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("private DNS detail", request=request)

    with pytest.raises(GdeltNetworkError) as error:
        _run(_client(handler).fetch_articles(query="gold"))
    assert calls == 1
    assert error.value.category is GdeltErrorCategory.NETWORK
    assert error.value.request_sent is False
    assert "private DNS detail" not in str(error.value)


class _SearchClient:
    def __init__(
        self,
        results: Mapping[str, tuple[Mapping[str, Any], ...]],
        failure: Exception | None = None,
    ) -> None:
        self.results = results
        self.failure = failure
        self.calls: list[str] = []

    async def fetch_articles(self, *, query: str):
        self.calls.append(query)
        if self.failure is not None:
            raise self.failure
        return self.results.get(query, ())


class _BlockingSearchClient(_SearchClient):
    def __init__(self, results: Mapping[str, tuple[Mapping[str, Any], ...]]) -> None:
        super().__init__(results)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_articles(self, *, query: str):
        self.calls.append(query)
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        return self.results.get(query, ())


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def _no_sleep(seconds: float) -> None:
    assert seconds >= 0


def test_gdelt_service_deduplicates_filters_and_reaches_scout_atlas_master() -> None:
    results: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for index, (query, _) in enumerate(GDELT_SEARCHES, start=1):
        results[query] = (_article(index),)
    first_query = GDELT_SEARCHES[0][0]
    second_query = GDELT_SEARCHES[1][0]
    results[second_query] = (
        _article(1),
        _article(20, title="Technology company releases a phone"),
    )
    client = _SearchClient(results)
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(
        query="Analyse Gold news Federal Reserve inflation dollar yields geopolitical risk"
    )

    diagnostic = _run(service.prepare(request))
    response = core.analyze(request, news_retrieval=diagnostic)

    assert client.calls == [query for query, _ in GDELT_SEARCHES]
    assert diagnostic.request_count == 5
    assert diagnostic.received_count == 6
    assert diagnostic.validated_count == 6
    assert diagnostic.duplicate_count == 1
    assert diagnostic.relevant_count == 4
    assert diagnostic.provider_id == "gdelt_doc_2"
    assert response.news_intelligence is not None
    assert len(response.news_intelligence.relevant_news) == 4
    assert "Technology company releases a phone" not in {
        item.headline for item in response.news_intelligence.relevant_news
    }
    assert all(
        item.search_category
        for item in response.news_intelligence.relevant_news
    )
    assert all(item.published_at is None for item in response.news_intelligence.relevant_news)
    assert all(item.seen_at is not None for item in response.news_intelligence.relevant_news)
    assert all(item.timestamp_basis == "provider_seen" for item in response.news_intelligence.relevant_news)
    statuses = {item.provider_id: item for item in response.news_intelligence.news_provider_statuses}
    assert statuses["gdelt_doc_2"].status is NewsRetrievalStatus.AVAILABLE
    assert statuses["alpha_vantage_news"].status is NewsRetrievalStatus.NOT_REQUESTED
    assert statuses["alpha_vantage_news"].request_sent is False
    scout = next(item for item in response.agent_results if item.agent_id == "scout_recon")
    atlas = next(item for item in response.agent_results if item.agent_id == "atlas_macro")
    assert "validated relevant news" in scout.summary
    assert "evidence-linked macro" in atlas.summary
    assert "Verified facts remain distinct from possible implications" in response.synthesis.final_response
    assert response.news_intelligence.production_authorization_available is False
    assert response.news_intelligence.submission_allowed is False
    assert response.news_intelligence.execution_authority is False


def test_gdelt_service_marks_seen_time_stale_without_claiming_publication_time() -> None:
    query = GDELT_SEARCHES[0][0]
    client = _SearchClient(
        {query: (_article(seen_at=NOW - timedelta(days=4)),)}
    )
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse Gold news")))
    response = core.analyze(
        AnalyzeRequest(query="Analyse Gold news"), news_retrieval=diagnostic
    )

    assert diagnostic.status is NewsRetrievalStatus.PARTIAL
    assert diagnostic.stale_count == 1
    item = response.news_intelligence.relevant_news[0]  # type: ignore[union-attr]
    assert item.freshness is EvidenceFreshness.STALE
    assert item.published_at is None
    assert item.timestamp_basis == "provider_seen"


def test_gdelt_provider_failure_stages_nothing_and_remains_unavailable() -> None:
    error = GdeltNetworkError(
        "safe",
        provider_reason_code="CONNECTION_FAILED",
        failure_stage="request",
        request_sent=False,
    )
    client = _SearchClient({}, failure=error)
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse Gold news")))

    assert diagnostic.status is NewsRetrievalStatus.NETWORK_ERROR
    assert diagnostic.error_category == "network"
    assert diagnostic.request_count == 1
    assert core.evidence_snapshot().news_evidence == ()


def test_gdelt_rate_limit_does_not_retry_or_ingest_evidence() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={"articles": [_article()]},
        )

    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=_client(handler, now=lambda: NOW),
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse Gold news")))

    assert calls == 1
    assert diagnostic.status is NewsRetrievalStatus.RATE_LIMITED
    assert diagnostic.error_category == "rate_limit"
    assert diagnostic.http_status_code == 429
    assert diagnostic.http_status_class == "4xx"
    assert diagnostic.provider_reason_code == "HTTP_RATE_LIMIT"
    assert diagnostic.failure_stage == "provider_response"
    assert diagnostic.request_sent is True
    assert diagnostic.retry_after_seconds == 60
    assert diagnostic.request_count == 1
    assert diagnostic.received_count == 0
    assert diagnostic.validated_count == 0
    assert diagnostic.evidence_ids == ()
    assert core.evidence_snapshot().news_evidence == ()


def test_rate_limit_without_cache_propagates_absence_cause_to_agents() -> None:
    error = GdeltRateLimitError(
        "safe",
        http_status_code=429,
        provider_reason_code="HTTP_RATE_LIMIT",
        failure_stage="provider_response",
        request_sent=True,
    )
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=_SearchClient({}, failure=error),  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news Federal Reserve risk")

    diagnostic = _run(service.prepare(request))
    response = core.analyze(request, news_retrieval=diagnostic)

    assert response.news_intelligence is not None
    assert response.news_intelligence.relevant_news == ()
    scout = next(item for item in response.agent_results if item.agent_id == "scout_recon")
    atlas = next(item for item in response.agent_results if item.agent_id == "atlas_macro")
    sentinel = next(
        item for item in response.agent_results if item.agent_id == "sentinel_risk_officer"
    )
    assert "provider status=rate_limited" in scout.summary
    assert "provider status=rate_limited" in atlas.summary
    assert "news provider status=rate_limited" in sentinel.summary
    assert "Provider status=rate_limited" in response.synthesis.final_response


def test_gdelt_unspecified_cooldown_blocks_later_explicit_retrievals() -> None:
    error = GdeltRateLimitError(
        "safe",
        http_status_code=429,
        provider_reason_code="HTTP_RATE_LIMIT",
        failure_stage="provider_response",
        request_sent=True,
    )
    client = _SearchClient({}, failure=error)
    service = GdeltNewsIntelligenceRetrievalService(
        core=create_friday_core(),
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news")

    async def scenario():
        return await service.prepare(request), await service.prepare(request)

    first, second = _run(scenario())

    assert len(client.calls) == 1
    assert first.status is NewsRetrievalStatus.RATE_LIMITED
    assert first.cooldown_status is NewsCooldownStatus.UNSPECIFIED
    assert first.cooldown_until is None
    assert second.status is NewsRetrievalStatus.RATE_LIMITED
    assert second.cooldown_status is NewsCooldownStatus.UNSPECIFIED
    assert second.request_count == 0
    assert second.request_sent is False
    assert second.reason_codes == ("GDELT_COOLDOWN_UNSPECIFIED",)


def test_gdelt_retry_after_cooldown_requires_a_later_explicit_request() -> None:
    clock = _Clock(NOW)
    error = GdeltRateLimitError(
        "safe",
        http_status_code=429,
        provider_reason_code="HTTP_RATE_LIMIT",
        failure_stage="provider_response",
        request_sent=True,
        retry_after_seconds=60,
    )
    client = _SearchClient({}, failure=error)
    service = GdeltNewsIntelligenceRetrievalService(
        core=create_friday_core(),
        client=client,  # type: ignore[arg-type]
        now=clock,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news")

    async def scenario():
        first = await service.prepare(request)
        clock.value = NOW + timedelta(seconds=30)
        blocked = await service.prepare(request)
        calls_before_expiry = len(client.calls)
        clock.value = NOW + timedelta(seconds=60)
        explicit_after_expiry = await service.prepare(request)
        return first, blocked, calls_before_expiry, explicit_after_expiry

    first, blocked, calls_before_expiry, explicit_after_expiry = _run(scenario())

    assert first.cooldown_status is NewsCooldownStatus.ACTIVE
    assert first.cooldown_until == NOW + timedelta(seconds=60)
    assert blocked.cooldown_status is NewsCooldownStatus.ACTIVE
    assert blocked.retry_after_seconds == 30
    assert blocked.request_count == 0
    assert calls_before_expiry == 1
    assert len(client.calls) == 2
    assert explicit_after_expiry.request_count == 1


def test_concurrent_gdelt_retrievals_share_one_sequential_operation() -> None:
    results = {
        query: (_article(index),)
        for index, (query, _) in enumerate(GDELT_SEARCHES, start=1)
    }
    client = _BlockingSearchClient(results)
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news")

    async def scenario():
        first = asyncio.create_task(service.prepare(request))
        await client.started.wait()
        second = asyncio.create_task(service.prepare(request))
        await asyncio.sleep(0)
        client.release.set()
        return await asyncio.gather(first, second)

    first, second = _run(scenario())

    assert client.calls == [query for query, _ in GDELT_SEARCHES]
    assert first == second
    assert first.request_count == len(GDELT_SEARCHES)
    assert len(core.evidence_snapshot().news_evidence) == len(GDELT_SEARCHES)


def test_rate_limit_preserves_fresh_cache_and_propagates_status_to_agents() -> None:
    results = {query: (_article(),) for query, _ in GDELT_SEARCHES}
    client = _SearchClient(results)
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news Federal Reserve")

    first = _run(service.prepare(request))
    before = core.evidence_snapshot().news_evidence
    client.failure = GdeltRateLimitError(
        "safe",
        http_status_code=429,
        provider_reason_code="HTTP_RATE_LIMIT",
        failure_stage="provider_response",
        request_sent=True,
    )
    limited = _run(service.prepare(request))
    after = core.evidence_snapshot().news_evidence
    response = core.analyze(request, news_retrieval=limited)

    assert first.newly_retrieved_evidence_count == 1
    assert len(before) == 1
    assert after == before
    assert limited.status is NewsRetrievalStatus.RATE_LIMITED
    assert limited.cache_status is NewsCacheStatus.FRESH
    assert limited.cached_evidence_count == 1
    assert limited.cached_evidence_ids == (before[0].provenance.event_id,)
    assert response.news_intelligence is not None
    assert len(response.news_intelligence.relevant_news) == 1
    assert response.news_intelligence.relevant_news[0].evidence_origin is NewsEvidenceOrigin.CACHED
    assert response.news_intelligence.relevant_news[0].retrieved_at == before[0].provenance.received_at
    scout = next(item for item in response.agent_results if item.agent_id == "scout_recon")
    atlas = next(item for item in response.agent_results if item.agent_id == "atlas_macro")
    assert "1 cached" in scout.summary
    assert "provider status=rate_limited" in scout.summary
    assert "provider status=rate_limited" in atlas.summary
    assert "Provider status=rate_limited" in response.synthesis.final_response
    assert response.news_intelligence.production_authorization_available is False
    assert response.news_intelligence.submission_allowed is False
    assert response.news_intelligence.execution_authority is False


def test_rate_limit_reports_stale_cache_without_refreshing_timestamps() -> None:
    initial_time = NOW - timedelta(days=4)
    clock = _Clock(initial_time)
    article = _article(seen_at=initial_time - timedelta(hours=1))
    client = _SearchClient({query: (article,) for query, _ in GDELT_SEARCHES})
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=clock,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news")

    _run(service.prepare(request))
    before = core.evidence_snapshot().news_evidence[0]
    clock.value = NOW
    client.failure = GdeltRateLimitError(
        "safe",
        http_status_code=429,
        provider_reason_code="HTTP_RATE_LIMIT",
        failure_stage="provider_response",
        request_sent=True,
    )
    limited = _run(service.prepare(request))
    after = core.evidence_snapshot().news_evidence[0]
    response = core.analyze(request, news_retrieval=limited)

    assert after == before
    assert limited.cache_status is NewsCacheStatus.STALE
    assert response.news_intelligence is not None
    assert response.news_intelligence.relevant_news[0].freshness is EvidenceFreshness.STALE
    assert response.news_intelligence.relevant_news[0].evidence_origin is NewsEvidenceOrigin.CACHED
    assert any("stale cached news evidence" in item for item in response.news_intelligence.warnings)


def test_successful_empty_gdelt_result_keeps_cache_distinct() -> None:
    client = _SearchClient({query: (_article(),) for query, _ in GDELT_SEARCHES})
    core = create_friday_core()
    service = GdeltNewsIntelligenceRetrievalService(
        core=core,
        client=client,  # type: ignore[arg-type]
        now=lambda: NOW,
        pacing_seconds=0,
        sleeper=_no_sleep,
    )
    request = AnalyzeRequest(query="Analyse Gold news")

    _run(service.prepare(request))
    client.results.clear()
    empty = _run(service.prepare(request))
    response = core.analyze(request, news_retrieval=empty)

    assert empty.status is NewsRetrievalStatus.PARTIAL
    assert empty.received_count == 0
    assert empty.validated_count == 0
    assert empty.reason_codes == (
        "GDELT_NEWS_RETRIEVED",
        "NO_RELEVANT_XAU_USD_NEWS",
    )
    assert empty.cache_status is NewsCacheStatus.FRESH
    assert empty.cached_evidence_count == 1
    assert response.news_intelligence is not None
    assert response.news_intelligence.relevant_news[0].evidence_origin is NewsEvidenceOrigin.CACHED
    assert any("returned no new article metadata" in item for item in response.news_intelligence.warnings)


def test_primary_factory_is_credential_free_and_does_not_construct_alpha_vantage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.connectors.alpha_vantage as alpha_module
    import app.services.friday_core as core_module

    def forbidden_alpha(*args, **kwargs):
        raise AssertionError("Alpha Vantage must not be constructed automatically")

    core = create_friday_core()
    monkeypatch.setattr(
        alpha_module.AlphaVantageNewsClient,
        "from_environment",
        forbidden_alpha,
    )
    monkeypatch.setattr(core_module, "get_friday_core", lambda: core)
    get_news_intelligence_retrieval_service.cache_clear()
    try:
        service = get_news_intelligence_retrieval_service()
    finally:
        get_news_intelligence_retrieval_service.cache_clear()

    assert isinstance(service, GdeltNewsIntelligenceRetrievalService)
