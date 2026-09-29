"""Phase 5A tests for provider-neutral historical market-series evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import inspect
import math

import pytest
from pydantic import ValidationError

from app.agents.base import AnalysisContext
from app.agents.orchestrator import create_friday_core
from app.agents.specialists import ChartistTechnicianAgent, OracleQuantAgent
from app.evidence import (
    AgentEvidence,
    EvidenceRouter,
    EvidenceSnapshot,
    EvidenceStore,
    MarketSeriesStore,
    NINE_SPECIALIST_IDS,
)
from app.evidence.models import MarketSeriesEvidence
from app.ingestion.contracts import MarketBarConsumer
from app.main import app
from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    NormalizedNewsResearchEvent,
    SourceMetadata,
    SourceTransport,
)
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
    timeframe_seconds,
)
from app.models.schemas import AnalyzeRequest
from app.services.market_series import MarketSeriesIngestionService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _bar(
    index: int = 0,
    *,
    symbol: str = "XAU/USD",
    source_id: str = "synthetic_history",
    venue: str | None = "TEST",
    timeframe: str = "1h",
    close: float | None = None,
    volume: float | None = 100.0,
    volume_kind: VolumeKind = VolumeKind.TICK,
    state: MarketBarState = MarketBarState.COMPLETED,
    received_offset: int = 60,
    revision: int | None = None,
) -> NormalizedMarketBar:
    duration = timedelta(seconds=timeframe_seconds(timeframe))
    opened_at = MOMENT + (duration * index)
    closed_at = opened_at + duration
    close_value = float(101 + index if close is None else close)
    open_value = float(100 + index)
    return NormalizedMarketBar(
        source=MarketSeriesSource(
            source_id=source_id,
            display_name=f"Synthetic {source_id}",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        ),
        symbol=symbol,
        asset_class=AssetClass.COMMODITY,
        venue=venue,
        timeframe=timeframe,
        opened_at=opened_at,
        closed_at=closed_at,
        open=open_value,
        high=max(open_value, close_value) + 1,
        low=min(open_value, close_value) - 1,
        close=close_value,
        volume=volume,
        volume_kind=volume_kind,
        state=state,
        received_at=closed_at + timedelta(seconds=received_offset),
        revision=revision,
    )


def _market_event() -> NormalizedDataEvent:
    return NormalizedDataEvent(
        event_id="market-event-1",
        source=SourceMetadata(
            source_id="synthetic_event",
            display_name="Synthetic Event",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        ),
        event_type=EventType.MARKET_CANDLE,
        event="bar closed",
        asset_class=AssetClass.COMMODITY,
        symbol="XAUUSD",
        timeframe="1h",
        open=100,
        high=102,
        low=99,
        close=101,
        timestamp=MOMENT,
        received_at=MOMENT,
        raw_payload={"provider_boundary_only": True},
    )


def _news_event() -> NormalizedNewsResearchEvent:
    return NormalizedNewsResearchEvent(
        event_id="news-event-1",
        source=SourceMetadata(
            source_id="synthetic_news",
            display_name="Synthetic News",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.FINANCIAL_NEWS,
        ),
        provider="Synthetic Publisher",
        title="Gold context",
        url="https://example.test/gold",
        timestamp=MOMENT,
        symbols=["XAUUSD"],
        summary="A provider-neutral research summary.",
        received_at=MOMENT,
    )


def _series(*bars: NormalizedMarketBar) -> MarketSeriesEvidence:
    store = MarketSeriesStore()
    for bar in bars:
        store.ingest(bar)
    return store.snapshot(symbol=bars[0].symbol).series[0]


def test_normalized_bar_canonicalizes_identity_and_preserves_volume_semantics() -> None:
    bar = _bar()
    same_identity_revision = _bar(close=101.5, revision=2)

    assert bar.symbol == "XAUUSD"
    assert bar.timeframe == "1h"
    assert bar.volume_kind is VolumeKind.TICK
    assert bar.bar_id == same_identity_revision.bar_id
    assert bar.series_key == same_identity_revision.series_key
    assert bar.series_key.series_id.startswith("market-series-")


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"timeframe": "0h"}, "timeframe"),
        ({"timeframe": "hourly"}, "timeframe"),
        ({"symbol": " / "}, "symbol"),
        ({"volume": -1}, "volume"),
        ({"volume": None, "volume_kind": VolumeKind.REAL}, "volume kind"),
        ({"volume": 10, "volume_kind": VolumeKind.UNAVAILABLE}, "unavailable"),
        ({"high": 99}, "high"),
        ({"low": 102}, "low"),
        ({"close": math.inf}, "finite"),
    ],
)
def test_normalized_bar_rejects_invalid_values(
    updates: dict[str, object],
    match: str,
) -> None:
    payload = _bar().model_dump(exclude={"bar_id", "timeframe_seconds"})
    payload.update(updates)
    with pytest.raises((ValidationError, ValueError), match=match):
        NormalizedMarketBar.model_validate(payload)


def test_normalized_bar_requires_aware_exact_timing_and_valid_completed_receipt() -> None:
    payload = _bar().model_dump(exclude={"bar_id", "timeframe_seconds"})

    with pytest.raises(ValidationError, match="timezone"):
        NormalizedMarketBar.model_validate({**payload, "opened_at": MOMENT.replace(tzinfo=None)})
    with pytest.raises(ValidationError, match="duration"):
        NormalizedMarketBar.model_validate(
            {**payload, "closed_at": MOMENT + timedelta(minutes=30)}
        )
    with pytest.raises(ValidationError, match="received before"):
        NormalizedMarketBar.model_validate(
            {**payload, "received_at": MOMENT + timedelta(minutes=30)}
        )


def test_normalized_bar_converts_aware_timestamps_to_utc() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    payload = _bar().model_dump(exclude={"bar_id", "timeframe_seconds"})
    payload.update(
        opened_at=MOMENT.astimezone(offset),
        closed_at=(MOMENT + timedelta(hours=1)).astimezone(offset),
        received_at=(MOMENT + timedelta(hours=1, minutes=1)).astimezone(offset),
    )
    bar = NormalizedMarketBar.model_validate(payload)
    assert bar.opened_at == MOMENT
    assert bar.opened_at.tzinfo is UTC


def test_normalized_bar_and_nested_source_are_immutable() -> None:
    bar = _bar()
    with pytest.raises(ValidationError):
        bar.close = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        bar.source.display_name = "Changed"  # type: ignore[misc]


def test_store_orders_deduplicates_and_resolves_explicit_revisions() -> None:
    store = MarketSeriesStore()
    revision_one = _bar(1, close=102, revision=1, received_offset=120)
    revision_two = _bar(1, close=103, revision=2, received_offset=60)
    for bar in (_bar(2), revision_two, _bar(0), revision_one, revision_two):
        store.ingest(bar)

    series = store.snapshot(symbol="XAUUSD").series[0]
    assert tuple(bar.opened_at for bar in series.bars) == tuple(
        sorted(bar.opened_at for bar in series.bars)
    )
    assert series.bar_count == 3
    assert series.bars[1].close == 103
    assert series.bars[1].revision == 2
    assert store.get_series(revision_two.series_key) == series


def test_unversioned_revision_uses_receipt_time_with_deterministic_tie_break() -> None:
    older = _bar(close=101, received_offset=60)
    newer = _bar(close=102, received_offset=120)
    first = MarketSeriesStore()
    second = MarketSeriesStore()
    for bar in (older, newer):
        first.ingest(bar)
    for bar in (newer, older):
        second.ingest(bar)
    assert first.snapshot() == second.snapshot()
    assert first.snapshot().series[0].bars[0].close == 102


def test_store_is_bounded_per_series_and_across_series() -> None:
    store = MarketSeriesStore(max_series=2, max_bars_per_series=2)
    for index in range(4):
        store.ingest(_bar(index))
    for source_id in ("source_b", "source_c"):
        store.ingest(_bar(source_id=source_id))

    snapshot = store.snapshot()
    assert store.series_count == 2
    assert all(series.bar_count <= 2 for series in snapshot.series)
    xau_series = next((series for series in snapshot.series if series.key.source_id == "synthetic_history"), None)
    if xau_series is not None:
        assert tuple(bar.opened_at for bar in xau_series.bars) == (
            MOMENT + timedelta(hours=2),
            MOMENT + timedelta(hours=3),
        )


def test_different_symbol_timeframe_and_source_create_separate_series() -> None:
    store = MarketSeriesStore(max_series=10)
    bars = (
        _bar(symbol="XAUUSD"),
        _bar(symbol="EURUSD"),
        _bar(symbol="XAUUSD", timeframe="2h"),
        _bar(symbol="XAUUSD", source_id="other_source"),
        _bar(symbol="XAUUSD", venue="OTHER"),
    )
    for bar in bars:
        store.ingest(bar)

    snapshot = store.snapshot()
    assert store.series_count == 5
    assert len({series.series_id for series in snapshot.series}) == 5
    assert {series.key for series in snapshot.series} == {bar.series_key for bar in bars}


def test_max_series_eviction_is_deterministic() -> None:
    bars = (
        _bar(source_id="source_a"),
        _bar(source_id="source_b"),
        _bar(source_id="source_c"),
    )
    left = MarketSeriesStore(max_series=2)
    right = MarketSeriesStore(max_series=2)
    for bar in bars:
        left.ingest(bar)
    for bar in reversed(bars):
        right.ingest(bar)

    assert left.snapshot() == right.snapshot()
    assert len(left.snapshot().series) == 2


def test_store_reports_gaps_without_filling_bars() -> None:
    series = _series(_bar(0), _bar(2))
    assert series.bar_count == 2
    assert series.continuity.is_ordered is True
    assert series.continuity.is_contiguous is False
    assert series.continuity.missing_interval_count == 1
    assert len(series.continuity.gaps) == 1
    assert series.continuity.first_opened_at == MOMENT
    assert series.continuity.last_closed_at == MOMENT + timedelta(hours=3)


def test_store_completed_and_provisional_state_and_latest_completed_lookup() -> None:
    completed = _bar(0)
    provisional = _bar(1, state=MarketBarState.PROVISIONAL)
    store = MarketSeriesStore()
    store.ingest(provisional)
    store.ingest(completed)
    series = store.snapshot().series[0]

    assert series.completed_count == 1
    assert series.provisional_count == 1
    assert series.latest_completed_at == completed.closed_at
    assert store.latest_completed(completed.series_key) == (completed,)


def test_store_snapshot_is_deterministic_across_insertion_order() -> None:
    bars = (_bar(2), _bar(0), _bar(1))
    left = MarketSeriesStore()
    right = MarketSeriesStore()
    for bar in bars:
        left.ingest(bar)
    for bar in reversed(bars):
        right.ingest(bar)

    assert left.snapshot() == right.snapshot()
    assert left.snapshot().series[0].fingerprint == right.snapshot().series[0].fingerprint


def test_store_is_thread_safe_and_remains_bounded() -> None:
    store = MarketSeriesStore(max_bars_per_series=10)
    bars = tuple(_bar(index) for index in range(30))
    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(store.ingest, reversed(bars)))

    series = store.snapshot().series[0]
    assert store.bar_count == 10
    assert series.bars[0].opened_at == MOMENT + timedelta(hours=20)
    assert series.bars[-1].opened_at == MOMENT + timedelta(hours=29)


def test_evidence_store_coexists_with_event_news_and_series_semantics() -> None:
    store = EvidenceStore()
    store.ingest_market_event(_market_event())
    store.ingest_news_event(_news_event())
    store.ingest_market_bar(_bar())
    snapshot = store.snapshot(symbol="XAU/USD")

    assert len(snapshot.market_evidence) == 1
    assert len(snapshot.news_evidence) == 1
    assert len(snapshot.market_series) == 1
    assert "provider_boundary_only" not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.market_series[0].fingerprint = "changed"  # type: ignore[misc]


def test_series_routes_only_to_chartist_and_oracle() -> None:
    series = _series(_bar())
    snapshot = EvidenceSnapshot(market_series=(series,), symbol="XAUUSD")
    router = EvidenceRouter()

    for agent_id in NINE_SPECIALIST_IDS:
        assigned = router.route(agent_id=agent_id, snapshot=snapshot)
        if agent_id in {"chartist_technician", "oracle_quant"}:
            assert assigned.market_series == (series,)
            assert assigned.market_series is snapshot.market_series
        else:
            assert assigned.market_series == ()


def test_chartist_and_oracle_use_only_direct_series_facts() -> None:
    series = _series(_bar(0), _bar(2))
    context = AnalysisContext(
        symbol="XAUUSD",
        query="Review the validated history",
        evidence=AgentEvidence(market_series=(series,)),
    )
    rendered_facts: dict[str, str] = {}
    for agent in (ChartistTechnicianAgent(), OracleQuantAgent()):
        result = agent.analyze(context)
        artifact = agent.structured_analysis(
            context,
            parent_analysis_id="phase5a-test",
            evidence_fingerprint=series.fingerprint,
            created_at=MOMENT,
            result=result,
        )
        rendered_facts[agent.agent_id] = " ".join(
            fact.statement for fact in artifact.fact_observations
        )
        assert artifact.stance.value == "insufficient_data"
        assert {reference.event_id for reference in artifact.supporting_evidence} == {
            series.series_id
        }

    assert "validated 1h bar(s)" in rendered_facts["chartist_technician"]
    assert "missing_intervals=1" in rendered_facts["chartist_technician"]
    assert "direct finite values" in rendered_facts["oracle_quant"]
    forbidden = ("rsi", "macd", "moving average", "breakout", "long", "short")
    assert not any(term in " ".join(rendered_facts.values()).casefold() for term in forbidden)


def test_internal_ingestion_boundary_stages_bars_without_an_api_route() -> None:
    core = create_friday_core()
    service = MarketSeriesIngestionService(consumer=core)
    result = service.ingest(_bar())

    assert isinstance(core, MarketBarConsumer)
    assert isinstance(result, MarketSeriesEvidence)
    assert len(core.evidence_snapshot(symbol="XAUUSD").market_series) == 1
    assert not any("market-series" in getattr(route, "path", "") for route in app.routes)


def test_series_traceability_changes_bundle_fingerprint_without_weakening_event_contract() -> None:
    original = _series(_bar(revision=1, close=101))
    revised = _series(_bar(revision=2, close=102))
    original_bundle = build_evidence_bundle(
        EvidenceSnapshot(market_series=(original,), symbol="XAUUSD")
    )
    revised_bundle = build_evidence_bundle(
        EvidenceSnapshot(market_series=(revised,), symbol="XAUUSD")
    )

    assert original_bundle.references[0].event_id == original.series_id
    assert original_bundle.references[0].timestamp == original.latest_completed_at
    assert original_bundle.fingerprint != revised_bundle.fingerprint
    assert build_evidence_bundle(EvidenceSnapshot()).fingerprint == build_evidence_bundle(
        EvidenceSnapshot(market_series=())
    ).fingerprint


def test_series_does_not_create_trade_fields_or_satisfy_sentinel_market_requirement() -> None:
    core = create_friday_core()
    core.ingest_market_bar(_bar())

    result = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="Review the historical bars and risk")
    )

    assert result.proposal.parameters is not None
    assert result.proposal.parameters.direction is None
    assert result.proposal.parameters.reference_price is None
    assert result.proposal.parameters.stop_loss is None
    assert result.proposal.parameters.targets == ()
    assert result.sentinel_decision.decision.value == "insufficient_data"
    assert "market_evidence" in result.sentinel_decision.missing_requirements


def test_market_series_modules_are_provider_neutral() -> None:
    import app.evidence.models as evidence_models
    import app.evidence.series_store as series_store
    import app.models.market_series as market_series

    rendered = "\n".join(
        inspect.getsource(module).casefold()
        for module in (market_series, evidence_models, series_store)
    )
    assert "alpha_vantage" not in rendered
    assert "tradingview" not in rendered
    assert "twelve_data" not in rendered
