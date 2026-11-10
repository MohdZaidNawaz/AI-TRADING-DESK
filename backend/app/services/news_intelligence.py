"""Evidence-based news retrieval and deterministic macro interpretation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from math import ceil
import re
from typing import Protocol

from app.connectors.alpha_vantage import (
    AlphaVantageError,
    AlphaVantageErrorCategory,
    AlphaVantageNewsConnector,
    alpha_vantage_news_connector,
)
from app.connectors.gdelt import (
    GDELT_MAX_RECORDS,
    GDELT_NEWS_SOURCE,
    GdeltError,
    GdeltErrorCategory,
    GdeltNewsClient,
    GdeltNewsConnector,
    gdelt_news_connector,
)
from app.evidence.models import EconomicEventEvidence, EvidenceSnapshot, NewsEvidence
from app.models.economic_events import EconomicImpact, EconomicReleaseStatus, EconomicValue
from app.models.news_intelligence import (
    CalendarProviderStatus,
    EconomicCalendarItem,
    EventRisk,
    EvidenceFreshness,
    MacroCategory,
    MacroFinding,
    NewsEconomicIntelligenceReport,
    NewsCacheStatus,
    NewsCooldownStatus,
    NewsEvidenceOrigin,
    NewsIntelligenceItem,
    NewsRetrievalDiagnostic,
    NewsRetrievalStatus,
)
from app.models.research_requests import InstrumentResolutionStatus
from app.models.schemas import AnalyzeRequest
from app.services.news_research import XauUsdNewsRelevanceService, xau_usd_news_relevance_service

DEFAULT_NEWS_COUNT = 10
NEWS_FRESHNESS = timedelta(hours=48)
NEWS_LOOKBACK = timedelta(days=7)
_XAU_NEWS_FILTER = "FOREX:USD"
GDELT_REQUEST_PACING_SECONDS = 1.0
GDELT_SEARCHES = (
    ("(gold OR XAU OR bullion OR \"precious metals\")", "Gold and precious metals"),
    ("(\"Federal Reserve\" OR FOMC OR \"interest rates\")", "Federal Reserve and monetary policy"),
    ("(\"US inflation\" OR CPI OR PCE)", "US inflation and employment"),
    ("(\"US dollar\" OR DXY OR \"Treasury yields\")", "US dollar and Treasury yields"),
    (
        "(geopolitical OR sanctions OR conflict OR war) AND (gold OR bullion OR \"safe haven\")",
        "Geopolitical developments relevant to Gold",
    ),
)


class NewsCore(Protocol):
    def prepare_research_request(self, request: AnalyzeRequest): ...
    def ingest_news_event(self, event): ...
    def evidence_snapshot(self, *, symbol: str | None = None): ...


class NewsClient(Protocol):
    async def fetch_news(self, **kwargs): ...


def _retrieval_failure(category) -> tuple[NewsRetrievalStatus, str]:
    category_value = category.value
    return {
        "network": (
            NewsRetrievalStatus.NETWORK_ERROR,
            "ALPHA_VANTAGE_NEWS_NETWORK_ERROR",
        ),
        "authentication": (
            NewsRetrievalStatus.AUTHENTICATION_ERROR,
            "ALPHA_VANTAGE_NEWS_AUTHENTICATION_REJECTED",
        ),
        "invalid_request": (
            NewsRetrievalStatus.INVALID_REQUEST,
            "ALPHA_VANTAGE_NEWS_INVALID_REQUEST",
        ),
        "rate_limit": (
            NewsRetrievalStatus.RATE_LIMITED,
            "ALPHA_VANTAGE_NEWS_RATE_LIMITED",
        ),
        "timeout": (
            NewsRetrievalStatus.TIMEOUT,
            "ALPHA_VANTAGE_NEWS_TIMEOUT",
        ),
        "server_error": (
            NewsRetrievalStatus.SERVER_ERROR,
            "ALPHA_VANTAGE_NEWS_SERVER_ERROR",
        ),
        "malformed_response": (
            NewsRetrievalStatus.MALFORMED,
            "ALPHA_VANTAGE_NEWS_MALFORMED_RESPONSE",
        ),
        "provider_error": (
            NewsRetrievalStatus.PROVIDER_ERROR,
            "ALPHA_VANTAGE_NEWS_PROVIDER_ERROR",
        ),
    }[category_value]


class NewsIntelligenceRetrievalService:
    """Retrieve one validated XAU/USD news batch and stage only relevant items."""

    def __init__(
        self,
        *,
        core: NewsCore,
        client: NewsClient | None,
        connector: AlphaVantageNewsConnector = alpha_vantage_news_connector,
        relevance_service: XauUsdNewsRelevanceService = xau_usd_news_relevance_service,
        now=None,
    ) -> None:
        self._core = core
        self._client = client
        self._connector = connector
        self._relevance_service = relevance_service
        self._now = now or (lambda: datetime.now(UTC))

    async def prepare(self, request: AnalyzeRequest) -> NewsRetrievalDiagnostic:
        context = self._core.prepare_research_request(request)
        subject = context.primary_instrument
        if subject.status is not InstrumentResolutionStatus.RESOLVED:
            return self._diagnostic(
                NewsRetrievalStatus.NOT_REQUESTED,
                "NEWS_INSTRUMENT_NOT_RESOLVED",
                request_count=0,
                request_sent=False,
            )
        if subject.canonical_symbol != "XAU/USD":
            return self._diagnostic(
                NewsRetrievalStatus.UNSUPPORTED,
                "NEWS_SYMBOL_NOT_SUPPORTED_IN_PHASE_4",
                request_count=0,
                request_sent=False,
            )
        if self._client is None:
            return self._diagnostic(
                NewsRetrievalStatus.NOT_CONFIGURED,
                "ALPHA_VANTAGE_NEWS_NOT_CONFIGURED",
                error_category="configuration",
                provider_reason_code="PROVIDER_NOT_CONFIGURED",
                failure_stage="configuration",
                request_count=0,
                request_sent=False,
            )

        retrieved_at = self._now().astimezone(UTC)
        try:
            records = await self._client.fetch_news(
                tickers=_XAU_NEWS_FILTER,
                time_from=retrieved_at - NEWS_LOOKBACK,
                time_to=retrieved_at,
                sort="RELEVANCE",
            )
            # Validate the complete response before mutating shared evidence.
            events = tuple(
                self._connector.normalize(record).model_copy(
                    update={"received_at": retrieved_at}
                )
                for record in records
            )
            selected = self._relevance_service.select(
                events,
                requested_count=DEFAULT_NEWS_COUNT,
                sort="RELEVANCE",
                starts_at=retrieved_at - NEWS_LOOKBACK,
                ends_before=retrieved_at + timedelta(microseconds=1),
            )
        except AlphaVantageError as error:
            status, reason = _retrieval_failure(error.category)
            return self._diagnostic(
                status,
                reason,
                error_category=error.category.value,
                retrieved_at=retrieved_at,
                http_status_code=error.http_status_code,
                http_status_class=error.http_status_class,
                provider_reason_code=error.provider_reason_code,
                failure_stage=error.failure_stage,
                request_sent=(
                    True
                    if error.request_sent is None
                    and error.category is AlphaVantageErrorCategory.MALFORMED_RESPONSE
                    and error.failure_stage == "article_normalization"
                    else error.request_sent
                ),
                request_count=1,
            )

        unique_ids = {event.event_id for event in events}
        stale_count = sum(
            _freshness(event.timestamp, retrieved_at) is EvidenceFreshness.STALE
            for event in selected
        )
        for event in selected:
            self._core.ingest_news_event(event)
        status = (
            NewsRetrievalStatus.AVAILABLE
            if selected and stale_count == 0
            else NewsRetrievalStatus.PARTIAL
        )
        reasons = ["ALPHA_VANTAGE_NEWS_RETRIEVED"]
        if not selected:
            reasons.append("NO_RELEVANT_XAU_USD_NEWS")
        if stale_count:
            reasons.append("STALE_NEWS_PRESENT")
        return NewsRetrievalDiagnostic(
            status=status,
            requested_count=DEFAULT_NEWS_COUNT,
            received_count=len(records),
            validated_count=len(events),
            relevant_count=len(selected),
            duplicate_count=max(0, len(events) - len(unique_ids)),
            stale_count=stale_count,
            retrieved_at=retrieved_at,
            evidence_ids=tuple(event.event_id for event in selected),
            reason_codes=tuple(reasons),
            request_count=1,
            search_queries=(_XAU_NEWS_FILTER,),
            http_status_class="2xx",
            request_sent=True,
        )

    @staticmethod
    def _diagnostic(
        status: NewsRetrievalStatus,
        reason: str,
        *,
        error_category=None,
        retrieved_at: datetime | None = None,
        http_status_code: int | None = None,
        http_status_class: str | None = None,
        provider_reason_code: str | None = None,
        failure_stage: str | None = None,
        request_sent: bool | None = None,
        request_count: int = 0,
        search_queries: tuple[str, ...] = (),
    ) -> NewsRetrievalDiagnostic:
        return NewsRetrievalDiagnostic(
            status=status,
            requested_count=DEFAULT_NEWS_COUNT,
            retrieved_at=retrieved_at,
            reason_codes=(reason,),
            request_count=request_count,
            search_queries=search_queries,
            error_category=error_category,
            http_status_code=http_status_code,
            http_status_class=http_status_class,
            provider_reason_code=provider_reason_code,
            failure_stage=failure_stage,
            request_sent=request_sent,
        )


class GdeltNewsIntelligenceRetrievalService:
    """Run five bounded GDELT discovery searches as the primary news source."""

    def __init__(
        self,
        *,
        core: NewsCore,
        client: GdeltNewsClient,
        connector: GdeltNewsConnector = gdelt_news_connector,
        relevance_service: XauUsdNewsRelevanceService = xau_usd_news_relevance_service,
        now=None,
        pacing_seconds: float = GDELT_REQUEST_PACING_SECONDS,
        sleeper=asyncio.sleep,
    ) -> None:
        if pacing_seconds < 0:
            raise ValueError("GDELT request pacing cannot be negative.")
        self._core = core
        self._client = client
        self._connector = connector
        self._relevance_service = relevance_service
        self._now = now or (lambda: datetime.now(UTC))
        self._pacing_seconds = pacing_seconds
        self._sleeper = sleeper
        self._coordination_lock = asyncio.Lock()
        self._inflight: asyncio.Task[NewsRetrievalDiagnostic] | None = None
        self._cooldown_until: datetime | None = None
        self._cooldown_unspecified = False

    async def prepare(self, request: AnalyzeRequest) -> NewsRetrievalDiagnostic:
        context = self._core.prepare_research_request(request)
        subject = context.primary_instrument
        queries = tuple(query for query, _ in GDELT_SEARCHES)
        if subject.status is not InstrumentResolutionStatus.RESOLVED:
            return self._diagnostic(
                NewsRetrievalStatus.NOT_REQUESTED,
                "NEWS_INSTRUMENT_NOT_RESOLVED",
                queries=queries,
                request_sent=False,
            )
        if subject.canonical_symbol != "XAU/USD":
            return self._diagnostic(
                NewsRetrievalStatus.UNSUPPORTED,
                "NEWS_SYMBOL_NOT_SUPPORTED_IN_PHASE_4",
                queries=queries,
                request_sent=False,
            )
        return await self._coordinated_retrieval(queries)

    async def _coordinated_retrieval(
        self,
        queries: tuple[str, ...],
    ) -> NewsRetrievalDiagnostic:
        evaluated_at = self._now().astimezone(UTC)
        async with self._coordination_lock:
            cooldown = self._cooldown_diagnostic(evaluated_at, queries)
            if cooldown is not None:
                return cooldown
            task = self._inflight
            if task is None:
                task = asyncio.create_task(self._retrieve(queries))
                self._inflight = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._coordination_lock:
                    if self._inflight is task:
                        self._inflight = None

    async def _retrieve(
        self,
        queries: tuple[str, ...],
    ) -> NewsRetrievalDiagnostic:
        retrieved_at = self._now().astimezone(UTC)
        cached_before = self._cached_gdelt_evidence()
        cached_before_ids = {
            item.provenance.event_id for item in cached_before
        }
        attempted = 0
        raw_records: list[tuple[dict, str, str]] = []
        try:
            for index, (query, category) in enumerate(GDELT_SEARCHES):
                attempted += 1
                records = await self._client.fetch_articles(query=query)
                raw_records.extend((dict(record), query, category) for record in records)
                if index < len(GDELT_SEARCHES) - 1 and self._pacing_seconds:
                    await self._sleeper(self._pacing_seconds)
            events = tuple(
                self._connector.normalize(
                    record,
                    search_query=query,
                    relevance_category=category,
                ).model_copy(update={"received_at": retrieved_at})
                for record, query, category in raw_records
            )
            selected = self._relevance_service.select(
                events,
                requested_count=DEFAULT_NEWS_COUNT,
                sort="RELEVANCE",
                starts_at=retrieved_at - NEWS_LOOKBACK,
                ends_before=retrieved_at + timedelta(microseconds=1),
            )
        except GdeltError as error:
            cooldown_status, cooldown_until = self._record_rate_limit(
                error,
                retrieved_at,
            )
            cache_status, cached_ids = _cache_state(cached_before, retrieved_at)
            status, reason = _retrieval_failure(error.category)
            return self._diagnostic(
                status,
                reason.replace("ALPHA_VANTAGE", "GDELT"),
                queries=queries,
                request_count=attempted,
                error_category=error.category.value,
                retrieved_at=retrieved_at,
                http_status_code=error.http_status_code,
                http_status_class=error.http_status_class,
                provider_reason_code=error.provider_reason_code,
                failure_stage=error.failure_stage,
                retry_after_seconds=error.retry_after_seconds,
                cooldown_status=cooldown_status,
                cooldown_until=cooldown_until,
                cache_status=cache_status,
                cached_evidence_count=len(cached_ids),
                cached_evidence_ids=cached_ids,
                request_sent=(
                    True
                    if error.request_sent is None
                    and error.category.value == "malformed_response"
                    and error.failure_stage == "article_normalization"
                    else error.request_sent
                ),
            )

        unique_ids = {event.event_id for event in events}
        stale_count = sum(
            _freshness(event.timestamp, retrieved_at) is EvidenceFreshness.STALE
            for event in selected
        )
        for event in selected:
            self._core.ingest_news_event(event)
        selected_ids = tuple(event.event_id for event in selected)
        new_ids = tuple(
            event_id for event_id in selected_ids if event_id not in cached_before_ids
        )
        selected_cached_ids = tuple(
            event_id for event_id in selected_ids if event_id in cached_before_ids
        )
        if selected:
            routed_cached = tuple(
                item
                for item in cached_before
                if item.provenance.event_id in set(selected_cached_ids)
            )
        else:
            routed_cached = cached_before
            selected_cached_ids = tuple(
                item.provenance.event_id for item in cached_before
            )
        cache_status, selected_cached_ids = _cache_state(
            routed_cached,
            retrieved_at,
        )
        reasons = ["GDELT_NEWS_RETRIEVED"]
        if not selected:
            reasons.append("NO_RELEVANT_XAU_USD_NEWS")
        if stale_count:
            reasons.append("STALE_NEWS_PRESENT")
        return NewsRetrievalDiagnostic(
            status=(
                NewsRetrievalStatus.AVAILABLE
                if selected and stale_count == 0
                else NewsRetrievalStatus.PARTIAL
            ),
            provider_id=GDELT_NEWS_SOURCE.source_id,
            provider_name=GDELT_NEWS_SOURCE.display_name,
            request_count=attempted,
            search_queries=queries,
            requested_count=GDELT_MAX_RECORDS,
            received_count=len(raw_records),
            validated_count=len(events),
            relevant_count=len(selected),
            duplicate_count=max(0, len(events) - len(unique_ids)),
            stale_count=stale_count,
            retrieved_at=retrieved_at,
            evidence_ids=selected_ids,
            new_evidence_ids=new_ids,
            newly_retrieved_evidence_count=len(new_ids),
            cached_evidence_ids=selected_cached_ids,
            cached_evidence_count=len(selected_cached_ids),
            cache_status=cache_status,
            reason_codes=tuple(reasons),
            http_status_class="2xx",
            request_sent=True,
        )

    def _cooldown_diagnostic(
        self,
        evaluated_at: datetime,
        queries: tuple[str, ...],
    ) -> NewsRetrievalDiagnostic | None:
        if self._cooldown_unspecified:
            status = NewsCooldownStatus.UNSPECIFIED
            reason = "GDELT_COOLDOWN_UNSPECIFIED"
            retry_after = None
            cooldown_until = None
        elif self._cooldown_until is not None and evaluated_at < self._cooldown_until:
            status = NewsCooldownStatus.ACTIVE
            reason = "GDELT_COOLDOWN_ACTIVE"
            retry_after = max(
                0,
                ceil((self._cooldown_until - evaluated_at).total_seconds()),
            )
            cooldown_until = self._cooldown_until
        else:
            self._cooldown_until = None
            return None
        cached = self._cached_gdelt_evidence()
        cache_status, cached_ids = _cache_state(cached, evaluated_at)
        return self._diagnostic(
            NewsRetrievalStatus.RATE_LIMITED,
            reason,
            queries=queries,
            error_category="rate_limit",
            http_status_code=429,
            http_status_class="4xx",
            provider_reason_code="HTTP_RATE_LIMIT",
            failure_stage="provider_response",
            retry_after_seconds=retry_after,
            cooldown_status=status,
            cooldown_until=cooldown_until,
            cache_status=cache_status,
            cached_evidence_count=len(cached_ids),
            cached_evidence_ids=cached_ids,
            request_sent=False,
        )

    def _record_rate_limit(
        self,
        error: GdeltError,
        retrieved_at: datetime,
    ) -> tuple[NewsCooldownStatus, datetime | None]:
        if error.category is not GdeltErrorCategory.RATE_LIMIT:
            return NewsCooldownStatus.INACTIVE, None
        if error.retry_after_seconds is None:
            self._cooldown_unspecified = True
            self._cooldown_until = None
            return NewsCooldownStatus.UNSPECIFIED, None
        self._cooldown_unspecified = False
        self._cooldown_until = retrieved_at + timedelta(
            seconds=error.retry_after_seconds
        )
        return NewsCooldownStatus.ACTIVE, self._cooldown_until

    def _cached_gdelt_evidence(self) -> tuple[NewsEvidence, ...]:
        return tuple(
            item
            for item in self._core.evidence_snapshot().news_evidence
            if item.provenance.source_id == GDELT_NEWS_SOURCE.source_id
        )

    @staticmethod
    def _diagnostic(
        status: NewsRetrievalStatus,
        reason: str,
        *,
        queries: tuple[str, ...],
        request_count: int = 0,
        error_category=None,
        retrieved_at: datetime | None = None,
        http_status_code: int | None = None,
        http_status_class: str | None = None,
        provider_reason_code: str | None = None,
        failure_stage: str | None = None,
        retry_after_seconds: int | None = None,
        cooldown_status: NewsCooldownStatus = NewsCooldownStatus.INACTIVE,
        cooldown_until: datetime | None = None,
        cache_status: NewsCacheStatus = NewsCacheStatus.NONE,
        cached_evidence_count: int = 0,
        newly_retrieved_evidence_count: int = 0,
        cached_evidence_ids: tuple[str, ...] = (),
        new_evidence_ids: tuple[str, ...] = (),
        request_sent: bool | None = None,
    ) -> NewsRetrievalDiagnostic:
        return NewsRetrievalDiagnostic(
            status=status,
            provider_id=GDELT_NEWS_SOURCE.source_id,
            provider_name=GDELT_NEWS_SOURCE.display_name,
            request_count=request_count,
            search_queries=queries,
            requested_count=GDELT_MAX_RECORDS,
            retrieved_at=retrieved_at,
            reason_codes=(reason,),
            error_category=error_category,
            http_status_code=http_status_code,
            http_status_class=http_status_class,
            provider_reason_code=provider_reason_code,
            failure_stage=failure_stage,
            retry_after_seconds=retry_after_seconds,
            cooldown_status=cooldown_status,
            cooldown_until=cooldown_until,
            cache_status=cache_status,
            cached_evidence_count=cached_evidence_count,
            newly_retrieved_evidence_count=newly_retrieved_evidence_count,
            cached_evidence_ids=cached_evidence_ids,
            new_evidence_ids=new_evidence_ids,
            request_sent=request_sent,
        )


class NewsEconomicIntelligenceService:
    """Project verified evidence and make bounded, conditional interpretations."""

    def __init__(self, *, now=None) -> None:
        self._now = now or (lambda: datetime.now(UTC))

    def analyze(
        self,
        *,
        subject: str,
        snapshot: EvidenceSnapshot,
        retrieval: NewsRetrievalDiagnostic | None,
    ) -> NewsEconomicIntelligenceReport:
        evaluated_at = self._now().astimezone(UTC)
        diagnostic = retrieval or NewsRetrievalDiagnostic(
            status=NewsRetrievalStatus.NOT_REQUESTED,
            provider_id=GDELT_NEWS_SOURCE.source_id,
            provider_name=GDELT_NEWS_SOURCE.display_name,
            requested_count=0,
            reason_codes=("NEWS_RETRIEVAL_NOT_RUN",),
            request_sent=False,
        )
        news = tuple(
            _news_item(item, evaluated_at, diagnostic)
            for item in snapshot.news_evidence
        )
        economic = tuple(
            _calendar_item(item, evaluated_at) for item in snapshot.economic_events
        )
        upcoming = tuple(
            item
            for item in economic
            if item.status is EconomicReleaseStatus.SCHEDULED
            and item.scheduled_at >= evaluated_at
        )
        released = tuple(
            item
            for item in economic
            if item.status in {EconomicReleaseStatus.RELEASED, EconomicReleaseStatus.REVISED}
        )
        findings = _macro_findings(snapshot.news_evidence, snapshot.economic_events)
        risks = tuple(
            EventRisk(
                evidence_id=item.evidence_id,
                event_name=item.event_name,
                scheduled_at=item.scheduled_at,
                impact=item.impact,
                risk_statement=(
                    "This verified scheduled event may increase volatility; direction remains unknown."
                ),
            )
            for item in upcoming
            if item.impact in {EconomicImpact.HIGH, EconomicImpact.MEDIUM}
        )
        missing = []
        warnings = []
        if not news:
            missing.append("No relevant validated news evidence is available.")
        if not economic:
            missing.append("No configured economic-calendar provider is available.")
            warnings.append("Calendar events and release values were not fabricated.")
        missing.append(
            "No verified market-session calendar or session-status provider is available."
        )
        if any(item.freshness is EvidenceFreshness.STALE for item in news):
            warnings.append("One or more relevant articles are stale for current-market interpretation.")
        if any(item.freshness is EvidenceFreshness.FUTURE_DATED for item in news):
            warnings.append("Future-dated news evidence is not treated as current context.")
        if diagnostic.status is NewsRetrievalStatus.RATE_LIMITED:
            warnings.append(
                "The news provider is rate limited; missing new evidence does not prove that no relevant news exists."
            )
        if diagnostic.cooldown_status is NewsCooldownStatus.UNSPECIFIED:
            warnings.append(
                "The provider supplied no Retry-After duration, so this service instance will not issue another request automatically."
            )
        elif diagnostic.cooldown_status is NewsCooldownStatus.ACTIVE:
            warnings.append(
                "The provider cooldown remains active; another request requires a later explicit analysis after expiry."
            )
        if diagnostic.cache_status is NewsCacheStatus.STALE:
            warnings.append(
                "Only stale cached news evidence is available; original provider and retrieval timestamps were preserved."
            )
        elif diagnostic.cache_status is NewsCacheStatus.FRESH:
            warnings.append(
                "Previously validated cached news evidence is included with its original timestamps."
            )
        if (
            diagnostic.status is NewsRetrievalStatus.PARTIAL
            and diagnostic.received_count == 0
            and diagnostic.cached_evidence_count
        ):
            warnings.append(
                "The successful provider search returned no new article metadata; cached evidence is shown separately."
            )
        return NewsEconomicIntelligenceReport(
            subject=subject,
            evaluated_at=evaluated_at,
            news_retrieval=diagnostic,
            news_provider_statuses=_provider_statuses(diagnostic),
            calendar_provider_status=(
                CalendarProviderStatus.EVIDENCE_AVAILABLE
                if economic
                else CalendarProviderStatus.PROVIDER_UNAVAILABLE
            ),
            calendar_reason_codes=(
                ("VALIDATED_CALENDAR_EVIDENCE_PRESENT",)
                if economic
                else ("ECONOMIC_CALENDAR_PROVIDER_NOT_CONFIGURED",)
            ),
            calendar_error_category=(None if economic else "configuration"),
            relevant_news=news,
            upcoming_events=upcoming,
            released_events=released,
            macro_findings=findings,
            event_risks=risks,
            missing_information=tuple(missing),
            warnings=tuple(warnings),
        )


def _news_item(
    evidence: NewsEvidence,
    evaluated_at: datetime,
    diagnostic: NewsRetrievalDiagnostic,
) -> NewsIntelligenceItem:
    event_at = evidence.published_at or evidence.seen_at or evidence.provenance.event_timestamp
    timestamp_basis = (
        "original_publication"
        if evidence.published_at is not None
        else "provider_seen"
        if evidence.seen_at is not None
        else "provider_event"
    )
    return NewsIntelligenceItem(
        evidence_id=evidence.provenance.event_id,
        provider_id=evidence.provenance.source_id,
        provider_name=evidence.provider,
        source_name=evidence.provenance.source_name,
        headline=evidence.title,
        url=evidence.url,
        published_at=evidence.published_at,
        seen_at=evidence.seen_at,
        event_at=event_at,
        timestamp_basis=timestamp_basis,
        retrieved_at=evidence.provenance.received_at,
        freshness=_freshness(event_at, evaluated_at),
        evidence_origin=_evidence_origin(evidence, diagnostic),
        symbols=evidence.symbols,
        topics=evidence.topics,
        publisher_domain=evidence.publisher_domain,
        language=evidence.language,
        source_country=evidence.source_country,
        search_query=evidence.search_query,
        search_category=evidence.search_category,
        relevance_reasons=_relevance_reasons(evidence),
        summary=evidence.summary,
        sentiment_label=(
            evidence.overall_sentiment.label if evidence.overall_sentiment else None
        ),
    )


def _calendar_item(
    evidence: EconomicEventEvidence,
    evaluated_at: datetime,
) -> EconomicCalendarItem:
    return EconomicCalendarItem(
        evidence_id=evidence.event_id,
        occurrence_id=evidence.occurrence_id,
        event_name=evidence.event_name,
        country=evidence.country,
        currency=evidence.currency,
        scheduled_at=evidence.scheduled_at,
        released_at=evidence.released_at,
        status=evidence.release_status,
        impact=evidence.expected_impact,
        previous=evidence.previous,
        forecast=evidence.forecast,
        actual=evidence.actual,
        provider_id=evidence.provenance.source_id,
        provider_name=evidence.provenance.source_name,
        retrieved_at=evidence.received_at,
        freshness=_freshness(evidence.received_at, evaluated_at),
    )


_CATEGORIES = (
    (MacroCategory.CENTRAL_BANK, re.compile(r"\b(federal reserve|fed|fomc|central bank)\b", re.I)),
    (MacroCategory.INFLATION, re.compile(r"\b(inflation|cpi|pce)\b", re.I)),
    (MacroCategory.EMPLOYMENT, re.compile(r"\b(employment|payrolls?|unemployment|jobs?)\b", re.I)),
    (MacroCategory.INTEREST_RATES, re.compile(r"\b(interest rates?|rate (?:cut|hike|decision)|treasury yields?)\b", re.I)),
    (MacroCategory.US_DOLLAR, re.compile(r"\b(us dollar|u\.s\. dollar|usd|dxy)\b", re.I)),
    (MacroCategory.GEOPOLITICS, re.compile(r"\b(geopolitic|war|sanctions?|military escalation)\b", re.I)),
)


def _macro_findings(
    news: tuple[NewsEvidence, ...],
    events: tuple[EconomicEventEvidence, ...],
) -> tuple[MacroFinding, ...]:
    findings: list[MacroFinding] = []
    for item in news:
        searchable = " ".join((item.title, item.summary or "", *item.topics))
        for category, pattern in _CATEGORIES:
            if pattern.search(searchable):
                findings.append(
                    MacroFinding(
                        category=category,
                        verified_fact=(
                            _news_verified_fact(item)
                        ),
                        interpretation=_interpretation(category),
                        possible_gold_implication=_gold_implication(category),
                        evidence_ids=(item.provenance.event_id,),
                        confidence="medium",
                        uncertainty=(
                            "The article is relevant context; it does not prove price causation or direction."
                        ),
                    )
                )
                break
    for item in events:
        fact = f'{item.event_name} is {item.release_status.value} for {item.scheduled_at.isoformat()}.'
        if item.actual is not None:
            fact += f" Actual={_value_text(item.actual)}."
        if item.forecast is not None:
            fact += f" Forecast={_value_text(item.forecast)}."
        findings.append(
            MacroFinding(
                category=MacroCategory.ECONOMIC_RELEASE,
                verified_fact=fact,
                interpretation="The event is retained as calendar evidence with forecast and actual kept distinct.",
                possible_gold_implication="Gold volatility may rise around the event; direction remains unknown.",
                evidence_ids=(item.event_id,),
                confidence="medium",
                uncertainty="No causal or directional conclusion is inferred from the calendar record alone.",
            )
        )
    return tuple(findings)


def _value_text(value: EconomicValue) -> str:
    return f"{value.value} {value.scale.value} {value.unit.value}"


def _interpretation(category: MacroCategory) -> str:
    return {
        MacroCategory.CENTRAL_BANK: "Central-bank communication can alter expected policy paths.",
        MacroCategory.INFLATION: "Inflation evidence can affect real-rate and policy expectations.",
        MacroCategory.EMPLOYMENT: "Employment evidence can affect expected policy and growth paths.",
        MacroCategory.INTEREST_RATES: "Rate and yield evidence can affect the opportunity cost of holding gold.",
        MacroCategory.US_DOLLAR: "US-dollar conditions can be relevant to USD-denominated gold.",
        MacroCategory.GEOPOLITICS: "Geopolitical developments can alter risk demand and uncertainty.",
    }.get(category, "The verified evidence may affect macro expectations.")


def _gold_implication(category: MacroCategory) -> str:
    return (
        "This factor may affect gold sensitivity, but the evidence does not establish a bullish or bearish outcome."
    )


def _freshness(published_at: datetime, evaluated_at: datetime) -> EvidenceFreshness:
    if published_at > evaluated_at:
        return EvidenceFreshness.FUTURE_DATED
    return (
        EvidenceFreshness.FRESH
        if evaluated_at - published_at <= NEWS_FRESHNESS
        else EvidenceFreshness.STALE
    )


def _cache_state(
    evidence: tuple[NewsEvidence, ...],
    evaluated_at: datetime,
) -> tuple[NewsCacheStatus, tuple[str, ...]]:
    if not evidence:
        return NewsCacheStatus.NONE, ()
    evidence_ids = tuple(item.provenance.event_id for item in evidence)
    freshness = tuple(
        _freshness(
            item.published_at
            or item.seen_at
            or item.provenance.event_timestamp,
            evaluated_at,
        )
        for item in evidence
    )
    status = (
        NewsCacheStatus.FRESH
        if any(item is EvidenceFreshness.FRESH for item in freshness)
        else NewsCacheStatus.STALE
    )
    return status, evidence_ids


def _evidence_origin(
    evidence: NewsEvidence,
    diagnostic: NewsRetrievalDiagnostic,
) -> NewsEvidenceOrigin:
    evidence_id = evidence.provenance.event_id
    if evidence_id in diagnostic.new_evidence_ids:
        return NewsEvidenceOrigin.NEWLY_RETRIEVED
    if evidence_id in diagnostic.cached_evidence_ids:
        return NewsEvidenceOrigin.CACHED
    if evidence_id in diagnostic.evidence_ids:
        return NewsEvidenceOrigin.NEWLY_RETRIEVED
    return NewsEvidenceOrigin.CACHED


def _news_verified_fact(item: NewsEvidence) -> str:
    if item.published_at is not None:
        return (
            f'{item.provider} supplied original publication metadata for "{item.title}" '
            f"at {item.published_at.isoformat()}."
        )
    if item.seen_at is not None:
        return (
            f'GDELT indexed metadata for the headline "{item.title}" at '
            f"{item.seen_at.isoformat()}; original publication time and full article content "
            "were not supplied as verified evidence."
        )
    return (
        f'Normalized provider metadata retained the headline "{item.title}"; '
        "original publication time is unavailable."
    )


def _provider_statuses(
    primary: NewsRetrievalDiagnostic,
) -> tuple[NewsRetrievalDiagnostic, ...]:
    statuses = [primary]
    if primary.provider_id == GDELT_NEWS_SOURCE.source_id:
        statuses.append(
            NewsRetrievalDiagnostic(
                status=NewsRetrievalStatus.NOT_REQUESTED,
                provider_id="alpha_vantage_news",
                provider_name="Alpha Vantage News & Sentiment",
                requested_count=0,
                reason_codes=("OPTIONAL_PROVIDER_NOT_REQUESTED",),
                request_sent=False,
            )
        )
    return tuple(statuses)


def _relevance_reasons(item: NewsEvidence) -> tuple[str, ...]:
    text = " ".join((item.title, item.summary or "", *item.topics))
    reasons = [category.value for category, pattern in _CATEGORIES if pattern.search(text)]
    if any(symbol.upper() in {"XAU", "XAU/USD", "XAUUSD", "GLD", "IAU"} for symbol in item.symbols):
        reasons.insert(0, "direct_gold_symbol")
    if re.search(r"\b(gold|xau|bullion|precious metals?)\b", text, re.I):
        reasons.insert(0, "direct_gold_text")
    return tuple(dict.fromkeys(reasons)) or ("xau_usd_macro_context",)


@lru_cache
def get_news_intelligence_retrieval_service() -> GdeltNewsIntelligenceRetrievalService:
    """Compose credential-free GDELT as the sole automatic news provider."""
    from app.services.friday_core import get_friday_core

    return GdeltNewsIntelligenceRetrievalService(
        core=get_friday_core(),
        client=GdeltNewsClient(),
    )
