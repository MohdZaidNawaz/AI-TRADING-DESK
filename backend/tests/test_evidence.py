"""Unit tests for the provider-neutral Phase 2 evidence primitives."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agents import create_friday_core
from app.agents.base import AnalysisContext, BaseAgent
from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.evidence import (
    EvidenceRouter,
    EvidenceSnapshot,
    EvidenceStore,
    MarketEvidence,
    NINE_SPECIALIST_IDS,
    NewsEvidence,
    canonical_symbol,
    symbols_match,
)
from app.models.schemas import AgentAnalysis, AnalyzeRequest
from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    NormalizedNewsResearchEvent,
    SourceMetadata,
    SourceTransport,
)

MARKET_SOURCE = SourceMetadata(
    source_id="test_market_source",
    display_name="Test Market Source",
    transport=SourceTransport.WEBHOOK,
    domain=DataDomain.MARKET_DATA,
)
SECOND_MARKET_SOURCE = SourceMetadata(
    source_id="test_market_api",
    display_name="Test Market API",
    transport=SourceTransport.API,
    domain=DataDomain.MARKET_DATA,
)
NEWS_SOURCE = SourceMetadata(
    source_id="test_news_source",
    display_name="Test News Source",
    transport=SourceTransport.API,
    domain=DataDomain.FINANCIAL_NEWS,
)


def _market_event(
    *,
    event_id: str = "market-1",
    symbol: str = "XAUUSD",
    timestamp: datetime | None = None,
    source: SourceMetadata = MARKET_SOURCE,
    event: str = "price_alert",
    price: float = 2648.32,
) -> NormalizedDataEvent:
    moment = timestamp or datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    return NormalizedDataEvent(
        event_id=event_id,
        source=source,
        event_type=EventType.MARKET_ALERT,
        event=event,
        asset_class=AssetClass.COMMODITY,
        symbol=symbol,
        exchange="OANDA",
        timeframe="60",
        alert_name="Gold momentum",
        message="A normalized alert message.",
        price=price,
        open=2640.0,
        high=2650.0,
        low=2638.0,
        close=price,
        volume=1420.0,
        indicator_values={"z_score": 1.2, "momentum": 61.2},
        timestamp=moment,
        received_at=moment + timedelta(seconds=2),
        attributes={"provider_only_attribute": "must not reach agents"},
        raw_payload={"provider_secret": "must not reach agents"},
    )


def _news_event(
    *,
    event_id: str = "news-1",
    symbols: list[str] | None = None,
    timestamp: datetime | None = None,
    source: SourceMetadata = NEWS_SOURCE,
) -> NormalizedNewsResearchEvent:
    moment = timestamp or datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    return NormalizedNewsResearchEvent(
        event_id=event_id,
        source=source,
        provider="Example Financial Wire",
        title="Gold responds to the policy outlook",
        url=f"https://example.test/news/{event_id}",
        timestamp=moment,
        received_at=moment + timedelta(seconds=3),
        symbols=symbols or ["XAU/USD", "USD"],
        topics=["Commodities", "Monetary Policy"],
        authors=["Analyst One"],
        publisher_domain="example.test",
        category="Macro",
        image_url="https://example.test/images/gold.jpg",
        overall_sentiment={"score": 0.42, "label": "Bullish"},
        topic_relevance=[{"topic": "Commodities", "relevance_score": 0.91}],
        symbol_sentiment=[
            {
                "symbol": "XAU/USD",
                "relevance_score": 0.94,
                "sentiment_score": 0.4,
                "sentiment_label": "Bullish",
            }
        ],
        summary="A concise normalized summary only.",
        content="An article body that must not enter evidence.",
    )


def test_market_projection_excludes_raw_payload_and_provider_only_attributes() -> None:
    evidence = MarketEvidence.from_event(_market_event())

    assert evidence.provenance.event_id == "market-1"
    assert evidence.provenance.source_id == "test_market_source"
    assert evidence.symbol == "XAUUSD"
    assert evidence.indicator_values == (("momentum", 61.2), ("z_score", 1.2))
    assert not hasattr(evidence, "raw_payload")
    assert not hasattr(evidence, "attributes")
    assert "provider_secret" not in repr(evidence)


def test_news_projection_excludes_content_and_preserves_normalized_metadata() -> None:
    evidence = NewsEvidence.from_event(_news_event())

    assert evidence.provenance.event_id == "news-1"
    assert evidence.provider == "Example Financial Wire"
    assert evidence.url == "https://example.test/news/news-1"
    assert evidence.summary == "A concise normalized summary only."
    assert evidence.overall_sentiment is not None
    assert evidence.overall_sentiment.score == 0.42
    assert evidence.topic_relevance[0].topic == "Commodities"
    assert evidence.symbol_sentiment[0].symbol == "XAU/USD"
    assert not hasattr(evidence, "content")
    assert "article body" not in repr(evidence)


def test_evidence_values_and_snapshots_are_immutable() -> None:
    store = EvidenceStore()
    store.ingest_market_event(_market_event())
    snapshot = store.snapshot()

    with pytest.raises(FrozenInstanceError):
        snapshot.symbol = "EURUSD"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.market_evidence[0].symbol = "EURUSD"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.market_evidence[0].indicator_values.append(("new", 1.0))  # type: ignore[attr-defined]


def test_store_is_bounded_and_snapshot_order_is_deterministic() -> None:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    events = [
        _market_event(event_id="market-old", timestamp=base),
        _market_event(event_id="market-middle", timestamp=base + timedelta(minutes=1)),
        _market_event(event_id="market-new", timestamp=base + timedelta(minutes=2)),
    ]
    first_store = EvidenceStore(max_market_events=2)
    second_store = EvidenceStore(max_market_events=2)

    for event in events:
        first_store.ingest_market_event(event)
    for event in reversed(events):
        second_store.ingest_market_event(event)

    first_snapshot = first_store.snapshot()
    second_snapshot = second_store.snapshot()
    expected_ids = ("market-new", "market-middle")

    assert first_store.market_count == 2
    assert tuple(item.provenance.event_id for item in first_snapshot.market_evidence) == expected_ids
    assert tuple(item.provenance.event_id for item in second_snapshot.market_evidence) == expected_ids
    assert first_snapshot == second_snapshot


def test_news_storage_is_independently_bounded() -> None:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    store = EvidenceStore(max_news_events=2)

    store.ingest_news_event(_news_event(event_id="news-old", timestamp=base))
    store.ingest_news_event(
        _news_event(event_id="news-middle", timestamp=base + timedelta(minutes=1))
    )
    store.ingest_news_event(
        _news_event(event_id="news-new", timestamp=base + timedelta(minutes=2))
    )

    assert store.news_count == 2
    assert tuple(
        item.provenance.event_id for item in store.snapshot().news_evidence
    ) == ("news-new", "news-middle")


def test_duplicate_event_ids_do_not_create_duplicate_evidence() -> None:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    older = _market_event(event_id="duplicate", timestamp=base, event="older")
    newer = _market_event(
        event_id="duplicate",
        timestamp=base + timedelta(minutes=1),
        event="newer",
    )
    first_store = EvidenceStore()
    second_store = EvidenceStore()

    first_store.ingest_market_event(older)
    first_store.ingest_market_event(newer)
    second_store.ingest_market_event(newer)
    second_store.ingest_market_event(older)

    first_snapshot = first_store.snapshot()
    second_snapshot = second_store.snapshot()

    assert first_store.market_count == 1
    assert first_snapshot == second_snapshot
    assert first_snapshot.market_evidence[0].event == "newer"


def test_duplicate_ties_choose_deterministic_evidence_independent_of_insertion() -> None:
    moment = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    lower_price = _market_event(
        event_id="same-id",
        timestamp=moment,
        price=2600.0,
    )
    higher_price = _market_event(
        event_id="same-id",
        timestamp=moment,
        price=2700.0,
    )
    first_store = EvidenceStore()
    second_store = EvidenceStore()

    first_store.ingest_market_event(lower_price)
    first_store.ingest_market_event(higher_price)
    second_store.ingest_market_event(higher_price)
    second_store.ingest_market_event(lower_price)

    assert first_store.snapshot() == second_store.snapshot()
    assert first_store.market_count == 1


def test_store_supports_symbol_scoping_and_obvious_symbol_equivalence() -> None:
    store = EvidenceStore()
    store.ingest_market_event(_market_event(symbol="OANDA:XAUUSD"))
    store.ingest_market_event(_market_event(event_id="eur-market", symbol="EUR/USD"))
    store.ingest_news_event(_news_event(symbols=["XAU/USD"]))
    store.ingest_news_event(_news_event(event_id="eur-news", symbols=["EURUSD"]))

    snapshot = store.snapshot(symbol="xau/usd")

    assert snapshot.symbol == "XAUUSD"
    assert tuple(item.symbol for item in snapshot.market_evidence) == ("OANDA:XAUUSD",)
    assert tuple(item.provenance.event_id for item in snapshot.news_evidence) == ("news-1",)
    assert canonical_symbol("OANDA:XAUUSD") == "XAUUSD"
    assert symbols_match("XAUUSD", "XAU/USD")
    assert not symbols_match("XAUUSD", "EURUSD")


def test_snapshot_combines_multiple_provider_neutral_sources() -> None:
    store = EvidenceStore()
    store.ingest_market_event(_market_event(source=MARKET_SOURCE))
    store.ingest_market_event(
        _market_event(
            event_id="market-api",
            source=SECOND_MARKET_SOURCE,
            timestamp=datetime(2026, 9, 29, 12, 1, tzinfo=UTC),
        )
    )
    store.ingest_news_event(_news_event())

    snapshot = store.snapshot()

    assert {item.provenance.source_id for item in snapshot.market_evidence} == {
        "test_market_source",
        "test_market_api",
    }
    assert snapshot.news_evidence[0].provenance.source_id == "test_news_source"


def test_router_applies_the_nine_specialist_evidence_matrix() -> None:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    store = EvidenceStore()
    for index in range(4):
        store.ingest_market_event(
            _market_event(
                event_id=f"market-{index}",
                timestamp=base + timedelta(minutes=index),
            )
        )
    store.ingest_news_event(_news_event(event_id="news-1", timestamp=base))
    store.ingest_news_event(
        _news_event(event_id="news-2", timestamp=base + timedelta(minutes=1))
    )
    snapshot = store.snapshot()
    router = EvidenceRouter()

    expected_counts = {
        "atlas_macro": (4, 2),
        "capitol_smart_money": (3, 2),
        "scout_recon": (4, 2),
        "athena_analyst": (3, 2),
        "chartist_technician": (4, 0),
        "oracle_quant": (4, 0),
        "sentinel_risk_officer": (4, 2),
        "pilot_execution": (4, 2),
        "ledger_the_book": (4, 2),
    }

    assert NINE_SPECIALIST_IDS == tuple(expected_counts)
    assert "news" not in NINE_SPECIALIST_IDS
    for agent_id, (market_count, news_count) in expected_counts.items():
        evidence = router.route(agent_id=agent_id, snapshot=snapshot)
        assert len(evidence.market_evidence) == market_count
        assert len(evidence.news_evidence) == news_count

    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert len(ledger.provenance) == 6
    assert router.route(agent_id="pilot_execution", snapshot=snapshot).provenance == ()


def test_router_returns_safe_empty_evidence_for_an_empty_snapshot() -> None:
    router = EvidenceRouter()
    snapshot = EvidenceSnapshot()

    evidence = router.route(agent_id="atlas_macro", snapshot=snapshot)

    assert evidence.market_evidence == ()
    assert evidence.news_evidence == ()
    assert evidence.provenance == ()


def test_router_rejects_an_unknown_specialist_id() -> None:
    with pytest.raises(ValueError, match="Unknown FRIDAY specialist ID"):
        EvidenceRouter().route(agent_id="news", snapshot=EvidenceSnapshot())


def test_evidence_models_do_not_import_provider_implementations() -> None:
    import app.evidence.models as evidence_models

    source = Path(evidence_models.__file__).read_text(encoding="utf-8")

    assert "app.connectors" not in source
    assert "alpha_vantage" not in source
    assert "tradingview" not in source
    assert "twelve_data" not in source


def test_projection_is_independent_of_later_normalized_event_mutation() -> None:
    event = _market_event()
    store = EvidenceStore()
    store.ingest_market_event(event)

    event.indicator_values["momentum"] = -99.0
    event.raw_payload["provider_secret"] = "changed"
    snapshot = store.snapshot()

    assert snapshot.market_evidence[0].indicator_values == (
        ("momentum", 61.2),
        ("z_score", 1.2),
    )
    assert "provider_secret" not in repr(snapshot)


def test_market_evidence_rejects_an_ambiguous_naive_timestamp() -> None:
    event = _market_event(timestamp=datetime(2026, 9, 29, 12, 0))

    with pytest.raises(ValueError, match="timestamps must include a timezone"):
        MarketEvidence.from_event(event)


def test_news_store_is_bounded_and_excludes_content_only_article_body() -> None:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    first = _news_event(event_id="news-old", timestamp=base)
    newest = _news_event(event_id="news-new", timestamp=base + timedelta(minutes=1))
    content_only = NormalizedNewsResearchEvent(
        event_id="content-only",
        source=NEWS_SOURCE,
        provider="Example Financial Wire",
        title="A content-only normalized item",
        url="https://example.test/news/content-only",
        timestamp=base + timedelta(minutes=2),
        summary=None,
        content="This body must never enter a news evidence projection.",
    )
    store = EvidenceStore(max_news_events=2)
    store.ingest_news_event(first)
    store.ingest_news_event(newest)
    store.ingest_news_event(content_only)

    snapshot = store.snapshot()

    assert store.news_count == 2
    assert tuple(item.provenance.event_id for item in snapshot.news_evidence) == (
        "content-only",
        "news-new",
    )
    assert snapshot.news_evidence[0].summary is None
    assert "body must never" not in repr(snapshot.news_evidence[0])


def test_analysis_context_preserves_two_argument_compatibility_with_empty_evidence() -> None:
    context = AnalysisContext(symbol="XAUUSD", query="Review market context")

    assert context.evidence.market_evidence == ()
    assert context.evidence.news_evidence == ()
    assert context.lifecycle.trigger_type == "user_query"
    assert context.lifecycle.trigger_event_id is None
    assert context.lifecycle.prior_results == ()


class _RecordingSpecialist(BaseAgent):
    routing_keywords = frozenset()

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.name = agent_id
        self.expertise = "Test evidence recorder"
        self.contexts: list[AnalysisContext] = []

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        self.contexts.append(context)
        return self.result(
            summary=f"Recorded evidence for {self.agent_id}.",
            findings=self.evidence_findings(context),
            limitations=[],
            confidence=0.5,
        )


def test_core_routes_shared_evidence_to_each_specialist_and_ledger_lifecycle() -> None:
    specialists = [_RecordingSpecialist(agent_id) for agent_id in NINE_SPECIALIST_IDS]
    core = FridayCore(
        specialists=specialists,
        master=MasterAgent(),
        evidence_store=EvidenceStore(),
    )
    core.ingest_news_event(_news_event())
    core.ingest_event(_market_event())

    # A generic request selects every specialist against the shared snapshot.
    analysis = core.analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Give me a complete overview")
    )
    contexts = {agent.agent_id: agent.contexts[-1] for agent in specialists}

    assert len(analysis.agent_results) == 9
    assert len(contexts["atlas_macro"].evidence.market_evidence) == 1
    assert len(contexts["atlas_macro"].evidence.news_evidence) == 1
    assert len(contexts["capitol_smart_money"].evidence.market_evidence) == 1
    assert len(contexts["athena_analyst"].evidence.news_evidence) == 1
    assert contexts["chartist_technician"].evidence.news_evidence == ()
    assert contexts["oracle_quant"].evidence.news_evidence == ()
    assert len(contexts["scout_recon"].evidence.news_evidence) == 1
    assert len(contexts["sentinel_risk_officer"].evidence.market_evidence) == 1
    assert len(contexts["pilot_execution"].evidence.news_evidence) == 1
    assert contexts["pilot_execution"].lifecycle.prior_results == ()
    assert len(contexts["ledger_the_book"].evidence.provenance) == 2
    assert contexts["ledger_the_book"].lifecycle.trigger_type == "user_query"
    assert len(contexts["ledger_the_book"].lifecycle.prior_results) == 8
    assert "news" not in {agent.agent_id for agent in specialists}


def test_core_market_ingestion_passes_structured_trigger_evidence() -> None:
    specialists = [_RecordingSpecialist(agent_id) for agent_id in NINE_SPECIALIST_IDS]
    core = FridayCore(
        specialists=specialists,
        master=MasterAgent(),
        evidence_store=EvidenceStore(),
    )
    market_event = _market_event()

    core.ingest_event(market_event)
    chartist_context = next(
        agent.contexts[-1]
        for agent in specialists
        if agent.agent_id == "chartist_technician"
    )

    assert chartist_context.lifecycle.trigger_type == "market_event"
    assert chartist_context.lifecycle.trigger_event_id == "market-1"
    assert chartist_context.evidence.market_evidence[0].provenance.event_id == "market-1"


def test_active_specialists_consume_only_their_routed_evidence() -> None:
    core = create_friday_core()
    core.ingest_news_event(_news_event())

    market_analysis = core.ingest_event(_market_event())
    market_results = {
        result.agent_id: result for result in market_analysis.agent_results
    }
    chartist_findings = " ".join(market_results["chartist_technician"].findings)
    scout_findings = " ".join(market_results["scout_recon"].findings)
    ledger_findings = " ".join(market_results["ledger_the_book"].findings)

    assert "1 market item(s) and 0 news item(s)" in chartist_findings
    assert "1 market item(s) and 1 news item(s)" in scout_findings
    assert "Evidence provenance retained for 2 item(s)" in ledger_findings
    assert "Prior specialist-result metadata" in ledger_findings

    pilot_analysis = core.analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Review a broker execution order")
    )
    pilot = next(
        result
        for result in pilot_analysis.agent_results
        if result.agent_id == "pilot_execution"
    )
    pilot_findings = " ".join(pilot.findings)

    assert "disabled" in pilot.summary.lower()
    assert "1 market item(s) and 1 news item(s)" in pilot_findings
    assert "order placement" in pilot.limitations[0].lower()


def test_agents_and_evidence_remain_free_of_provider_implementation_imports() -> None:
    module_paths = (
        Path("app/agents/base.py"),
        Path("app/agents/specialists.py"),
        Path("app/evidence/models.py"),
        Path("app/evidence/router.py"),
    )
    backend_root = Path(__file__).resolve().parents[1]
    forbidden = ("app.connectors", "alpha_vantage", "tradingview", "twelve_data")

    for relative_path in module_paths:
        source = (backend_root / relative_path).read_text(encoding="utf-8").lower()
        assert not any(name in source for name in forbidden)
