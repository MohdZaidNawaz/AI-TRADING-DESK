from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.agents import FridayCore, create_friday_core
from app.agents.master import MasterAgent
from app.agents.specialists import create_specialist_agents
from app.connectors.alpha_vantage import (
    AlphaVantageAuthenticationError,
    AlphaVantageErrorCategory,
    AlphaVantageInvalidRequestError,
    AlphaVantageNetworkError,
    AlphaVantageNewsClient,
    AlphaVantagePayloadError,
    AlphaVantageProviderError,
    AlphaVantageRateLimitError,
    AlphaVantageServerError,
    AlphaVantageTimeoutError,
)
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
    EconomicValueUnit,
    NormalizedEconomicEvent,
)
from app.models.events import DataDomain, SourceTransport
from app.models.news_intelligence import (
    CalendarProviderStatus,
    EvidenceFreshness,
    NewsRetrievalStatus,
)
from app.models.schemas import AnalyzeRequest
from app.services.news_intelligence import (
    NewsEconomicIntelligenceService,
    NewsIntelligenceRetrievalService,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _run(value):
    return asyncio.run(value)


def _article(
    index: int,
    *,
    title: str = "Federal Reserve discusses inflation and interest rates",
    summary: str = "The policy discussion covered inflation, rates and the US dollar.",
    published: datetime | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    timestamp = published or (NOW - timedelta(hours=1))
    return {
        "title": title,
        "url": url or f"https://news.example.test/{index}",
        "time_published": timestamp.strftime("%Y%m%dT%H%M%S"),
        "authors": ["Reporter"],
        "summary": summary,
        "source": "Example Macro Wire",
        "source_domain": "news.example.test",
        "category_within_source": "Macro",
        "topics": [{"topic": "Economy - Monetary", "relevance_score": "0.9"}],
        "overall_sentiment_score": "0.1",
        "overall_sentiment_label": "Neutral",
        "ticker_sentiment": [
            {
                "ticker": "FOREX:USD",
                "relevance_score": "0.9",
                "ticker_sentiment_score": "0.1",
                "ticker_sentiment_label": "Neutral",
            }
        ],
    }


class _Client:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def fetch_news(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return tuple(self.result)


def _service(core, result):
    client = _Client(result)
    return NewsIntelligenceRetrievalService(
        core=core,
        client=client,
        now=lambda: NOW,
    ), client


def test_retrieval_filters_irrelevant_deduplicates_and_marks_stale_news() -> None:
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        news_intelligence_service=NewsEconomicIntelligenceService(now=lambda: NOW),
    )
    duplicate = _article(1, url="https://news.example.test/same")
    stale = _article(2, published=NOW - timedelta(days=3))
    irrelevant = _article(
        3,
        title="Technology company launches a new phone",
        summary="The product is available for purchase.",
    )
    irrelevant["topics"] = []
    irrelevant["ticker_sentiment"] = []
    service, client = _service(core, [duplicate, duplicate, stale, irrelevant])

    diagnostic = _run(service.prepare(AnalyzeRequest(query="What news could affect XAUUSD?")))

    assert len(client.calls) == 1
    assert client.calls[0]["tickers"] == "FOREX:USD"
    assert diagnostic.status is NewsRetrievalStatus.PARTIAL
    assert diagnostic.received_count == 4
    assert diagnostic.validated_count == 4
    assert diagnostic.relevant_count == 2
    assert diagnostic.duplicate_count == 1
    assert diagnostic.stale_count == 1
    assert len(diagnostic.evidence_ids) == 2
    response = core.analyze(
        AnalyzeRequest(query="What news could affect XAUUSD?"),
        news_retrieval=diagnostic,
    )
    assert response.news_intelligence is not None
    assert len(response.news_intelligence.relevant_news) == 2
    assert {item.freshness for item in response.news_intelligence.relevant_news} == {
        EvidenceFreshness.FRESH,
        EvidenceFreshness.STALE,
    }
    scout = next(item for item in response.agent_results if item.agent_id == "scout_recon")
    assert "validated relevant news" in scout.summary
    assert response.news_intelligence.production_authorization_available is False
    assert response.news_intelligence.submission_allowed is False
    assert response.news_intelligence.execution_authority is False


def test_malformed_article_rejects_the_complete_batch_before_staging() -> None:
    core = create_friday_core()
    malformed = _article(2)
    malformed.pop("time_published")
    service, _ = _service(core, [_article(1), malformed])

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse Gold news")))

    assert diagnostic.status is NewsRetrievalStatus.MALFORMED
    assert diagnostic.error_category == "malformed_response"
    assert diagnostic.failure_stage == "article_normalization"
    assert diagnostic.request_sent is True
    assert core.evidence_snapshot().news_evidence == ()


def test_macro_specialist_and_master_use_verified_news_without_claiming_causation() -> None:
    core = create_friday_core()
    service, _ = _service(core, [_article(1)])
    request = AnalyzeRequest(query="How could Federal Reserve inflation news affect Gold?")
    diagnostic = _run(service.prepare(request))

    response = core.analyze(request, news_retrieval=diagnostic)

    atlas = next(item for item in response.agent_results if item.agent_id == "atlas_macro")
    assert "evidence-linked macro" in atlas.summary
    assert any("Verified:" in finding for finding in atlas.findings)
    assert "News/economic evidence: 1 relevant article" in response.synthesis.final_response
    assert "do not establish price causation" in " ".join(atlas.limitations)
    assert response.mode == "deterministic_mock"


@pytest.mark.parametrize(
    ("error", "status", "category"),
    [
        (
            AlphaVantageRateLimitError(
                "safe",
                http_status_code=429,
                provider_reason_code="HTTP_RATE_LIMIT",
                failure_stage="http_response",
                request_sent=True,
            ),
            NewsRetrievalStatus.RATE_LIMITED,
            "rate_limit",
        ),
        (
            AlphaVantageTimeoutError(
                "safe",
                provider_reason_code="REQUEST_TIMEOUT",
                failure_stage="request",
                request_sent=None,
            ),
            NewsRetrievalStatus.TIMEOUT,
            "timeout",
        ),
        (
            AlphaVantagePayloadError(
                "safe",
                http_status_code=200,
                provider_reason_code="INVALID_JSON",
                failure_stage="response_decode",
                request_sent=True,
            ),
            NewsRetrievalStatus.MALFORMED,
            "malformed_response",
        ),
    ],
)
def test_provider_failures_are_sanitized_and_fail_closed(error, status, category) -> None:
    core = create_friday_core()
    service, _ = _service(core, error)

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse Gold news")))

    assert diagnostic.status is status
    assert diagnostic.error_category == category
    assert diagnostic.evidence_ids == ()
    assert core.evidence_snapshot().news_evidence == ()


@pytest.mark.parametrize(
    ("error", "status", "category", "reason"),
    [
        (
            AlphaVantageNetworkError("safe", provider_reason_code="CONNECTION_FAILED", failure_stage="request", request_sent=False),
            NewsRetrievalStatus.NETWORK_ERROR,
            "network",
            "ALPHA_VANTAGE_NEWS_NETWORK_ERROR",
        ),
        (
            AlphaVantageAuthenticationError("safe", http_status_code=401, provider_reason_code="HTTP_AUTHENTICATION_REJECTED", failure_stage="http_response", request_sent=True),
            NewsRetrievalStatus.AUTHENTICATION_ERROR,
            "authentication",
            "ALPHA_VANTAGE_NEWS_AUTHENTICATION_REJECTED",
        ),
        (
            AlphaVantageInvalidRequestError("safe", http_status_code=400, provider_reason_code="HTTP_INVALID_REQUEST", failure_stage="http_response", request_sent=True),
            NewsRetrievalStatus.INVALID_REQUEST,
            "invalid_request",
            "ALPHA_VANTAGE_NEWS_INVALID_REQUEST",
        ),
        (
            AlphaVantageServerError("safe", http_status_code=503, provider_reason_code="HTTP_SERVER_ERROR", failure_stage="http_response", request_sent=True),
            NewsRetrievalStatus.SERVER_ERROR,
            "server_error",
            "ALPHA_VANTAGE_NEWS_SERVER_ERROR",
        ),
        (
            AlphaVantageProviderError("safe", category=AlphaVantageErrorCategory.PROVIDER_ERROR, provider_reason_code="PROVIDER_MESSAGE_UNCLASSIFIED", failure_stage="provider_message", request_sent=True),
            NewsRetrievalStatus.PROVIDER_ERROR,
            "provider_error",
            "ALPHA_VANTAGE_NEWS_PROVIDER_ERROR",
        ),
    ],
)
def test_service_preserves_safe_structured_failure_metadata(
    error,
    status,
    category,
    reason,
) -> None:
    core = create_friday_core()
    service, _ = _service(core, error)

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse Gold news")))

    assert diagnostic.status is status
    assert diagnostic.error_category == category
    assert diagnostic.reason_codes == (reason,)
    assert diagnostic.provider_reason_code == error.provider_reason_code
    assert diagnostic.http_status_code == error.http_status_code
    assert diagnostic.http_status_class == error.http_status_class
    assert diagnostic.failure_stage == error.failure_stage
    assert diagnostic.request_sent is error.request_sent
    payload = diagnostic.model_dump(mode="json")
    assert "message" not in payload
    assert "url" not in payload
    assert "headers" not in payload


def test_alpha_vantage_client_classifies_a_transport_timeout_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("sensitive transport text", request=request)

    client = AlphaVantageNewsClient(
        "test-key-not-logged",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AlphaVantageTimeoutError, match="timed out"):
        _run(client.fetch_news(tickers="FOREX:USD"))
    assert calls == 1


def _economic_event(*, released: bool) -> NormalizedEconomicEvent:
    source = EconomicEventSource(
        source_id="verified-calendar-test",
        display_name="Verified Calendar Test",
        transport=SourceTransport.API,
        domain=DataDomain.ECONOMIC_DATA,
    )
    return NormalizedEconomicEvent(
        source=source,
        occurrence_id="us-cpi-2026-11",
        event_name="US Consumer Price Index",
        indicator_id="US_CPI",
        country="US",
        currency="USD",
        reference_period="2026-10",
        scheduled_at=(NOW - timedelta(hours=2) if released else NOW + timedelta(hours=2)),
        released_at=(NOW - timedelta(hours=2) if released else None),
        release_status=(
            EconomicReleaseStatus.RELEASED
            if released
            else EconomicReleaseStatus.SCHEDULED
        ),
        expected_impact=EconomicImpact.HIGH,
        previous=EconomicValue(value=Decimal("2.5"), unit=EconomicValueUnit.PERCENT),
        forecast=EconomicValue(value=Decimal("2.6"), unit=EconomicValueUnit.PERCENT),
        actual=(
            EconomicValue(value=Decimal("2.7"), unit=EconomicValueUnit.PERCENT)
            if released
            else None
        ),
        release_version=1,
        received_at=NOW,
    )


def test_calendar_and_macro_projection_keep_forecast_actual_and_event_risk_distinct() -> None:
    core = create_friday_core()
    core.ingest_economic_event(_economic_event(released=False))
    report = NewsEconomicIntelligenceService(now=lambda: NOW).analyze(
        subject="XAU/USD",
        snapshot=core.evidence_snapshot(symbol="XAU/USD"),
        retrieval=None,
    )

    assert report.calendar_provider_status is CalendarProviderStatus.EVIDENCE_AVAILABLE
    assert len(report.upcoming_events) == 1
    item = report.upcoming_events[0]
    assert item.forecast is not None and item.forecast.value == Decimal("2.6")
    assert item.actual is None
    assert len(report.event_risks) == 1
    assert any(finding.category.value == "economic_release" for finding in report.macro_findings)
    assert all("direction remains unknown" in risk.risk_statement for risk in report.event_risks)


def test_calendar_provider_absence_is_reported_without_fabricated_events() -> None:
    report = NewsEconomicIntelligenceService(now=lambda: NOW).analyze(
        subject="XAU/USD",
        snapshot=create_friday_core().evidence_snapshot(symbol="XAU/USD"),
        retrieval=None,
    )

    assert report.calendar_provider_status is CalendarProviderStatus.PROVIDER_UNAVAILABLE
    assert report.upcoming_events == ()
    assert report.released_events == ()
    assert "No configured economic-calendar provider is available." in report.missing_information


def test_released_calendar_data_preserves_forecast_and_actual_as_separate_values() -> None:
    core = create_friday_core()
    core.ingest_economic_event(_economic_event(released=True))
    report = NewsEconomicIntelligenceService(now=lambda: NOW).analyze(
        subject="XAU/USD",
        snapshot=core.evidence_snapshot(symbol="XAU/USD"),
        retrieval=None,
    )

    assert report.upcoming_events == ()
    assert len(report.released_events) == 1
    item = report.released_events[0]
    assert item.forecast is not None and item.forecast.value == Decimal("2.6")
    assert item.actual is not None and item.actual.value == Decimal("2.7")
    assert item.forecast != item.actual


def test_unsupported_instrument_makes_no_provider_request() -> None:
    core = create_friday_core()
    service, client = _service(core, [_article(1)])

    diagnostic = _run(service.prepare(AnalyzeRequest(query="Analyse BTCUSD news")))

    assert diagnostic.status is NewsRetrievalStatus.UNSUPPORTED
    assert client.calls == []
