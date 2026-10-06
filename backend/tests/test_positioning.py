"""Phase 5B-4 tests for provider-neutral positioning evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.base import AnalysisLifecycle
from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import EvidenceRouter, EvidenceStore, PositioningStore
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.positioning import (
    NormalizedPositioningObservation,
    ParticipantCategory,
    PositioningFrequency,
    PositioningMeasure,
    PositioningReferencePeriod,
    PositioningReferencePeriodKind,
    PositioningSeriesKey,
    PositioningSource,
)
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistStance
from app.services.positioning import PositioningIngestionService


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = PositioningSource(
    source_id="synthetic-positioning-source",
    display_name="Synthetic Positioning Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.POSITIONING_DATA,
)


def _key(
    *,
    source_id: str = SOURCE.source_id,
    source_series_id: str = "COT-GOLD-NET",
    instrument_id: str = "COMEX-GOLD",
    symbol: str | None = "XAUUSD",
    asset_class: AssetClass = AssetClass.COMMODITY,
    venue: str | None = "COMEX",
    participant_category: ParticipantCategory = ParticipantCategory.ASSET_MANAGER,
    measure: PositioningMeasure = PositioningMeasure.NET_POSITION,
    frequency: PositioningFrequency = PositioningFrequency.WEEKLY,
    unit: EconomicValueUnit = EconomicValueUnit.COUNT,
    scale: EconomicValueScale = EconomicValueScale.ONE,
) -> PositioningSeriesKey:
    return PositioningSeriesKey(
        source_id=source_id,
        source_series_id=source_series_id,
        instrument_id=instrument_id,
        symbol=symbol,
        asset_class=asset_class,
        venue=venue,
        participant_category=participant_category,
        measure=measure,
        frequency=frequency,
        value_unit=unit,
        value_scale=scale,
    )


def _period(
    index: int = 0,
    *,
    kind: PositioningReferencePeriodKind = PositioningReferencePeriodKind.WEEK,
) -> PositioningReferencePeriod:
    start = date(2026, 9, 1) + timedelta(days=index * 7)
    if kind is PositioningReferencePeriodKind.MONTH:
        return PositioningReferencePeriod(
            kind=kind,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            label="2026-09",
        )
    if kind is PositioningReferencePeriodKind.QUARTER:
        return PositioningReferencePeriod(
            kind=kind,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 9, 30),
            label="2026-Q3",
        )
    if kind is PositioningReferencePeriodKind.DATE:
        return PositioningReferencePeriod(kind=kind, start_date=start, end_date=start)
    return PositioningReferencePeriod(kind=kind, start_date=start, end_date=start + timedelta(days=6))


def _observation(
    index: int = 0,
    *,
    key: PositioningSeriesKey | None = None,
    source: PositioningSource = SOURCE,
    period: PositioningReferencePeriod | None = None,
    value: str = "125000",
    version: int = 1,
    revision_of_observation_id: str | None = None,
    received_at: datetime | None = None,
    observed_at: datetime | None = None,
    published_at: datetime | None = None,
    vintage_at: datetime | None = None,
) -> NormalizedPositioningObservation:
    actual_key = key or _key(source_id=source.source_id)
    reference = period or _period(index)
    initial = NormalizedPositioningObservation(
        source=source,
        series_key=actual_key,
        reference_period=reference,
        value=EconomicValue(
            value=Decimal(value),
            unit=actual_key.value_unit,
            scale=actual_key.value_scale,
        ),
        observation_version=1,
        received_at=received_at or MOMENT + timedelta(days=index, minutes=1),
        observed_at=observed_at,
        published_at=published_at,
        vintage_at=vintage_at,
    )
    if version == 1:
        return initial
    payload = initial.model_dump(round_trip=True)
    return NormalizedPositioningObservation.model_validate(
        {
            **payload,
            "observation_version": version,
            "revision_of_observation_id": revision_of_observation_id or initial.observation_id,
        }
    )


def test_contract_is_frozen_nested_and_excludes_raw_payloads() -> None:
    observation = _observation()
    with pytest.raises(ValidationError):
        observation.received_at = MOMENT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        observation.series_key.instrument_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        observation.reference_period.label = "changed"  # type: ignore[misc]
    payload = observation.model_dump(exclude={"observation_id", "fingerprint"})
    with pytest.raises(ValidationError):
        NormalizedPositioningObservation(**payload, raw_payload={"forbidden": True})
    with pytest.raises(ValidationError, match="finite"):
        EconomicValue(value=Decimal("NaN"), unit=EconomicValueUnit.COUNT)


def test_typed_value_unit_scale_and_utc_timestamp_normalization_are_preserved() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    observation = _observation(
        key=_key(unit=EconomicValueUnit.CURRENCY, scale=EconomicValueScale.MILLION),
        value="12.5",
        observed_at=MOMENT.astimezone(offset),
        published_at=(MOMENT + timedelta(hours=1)).astimezone(offset),
        vintage_at=(MOMENT + timedelta(hours=2)).astimezone(offset),
        received_at=MOMENT + timedelta(hours=3),
    )
    assert observation.value == EconomicValue(
        value=Decimal("12.5"),
        unit=EconomicValueUnit.CURRENCY,
        scale=EconomicValueScale.MILLION,
    )
    assert observation.observed_at == MOMENT
    assert observation.published_at == MOMENT + timedelta(hours=1)
    assert observation.vintage_at == MOMENT + timedelta(hours=2)
    payload = observation.model_dump(exclude={"observation_id", "fingerprint"})
    with pytest.raises(ValidationError, match="timezone"):
        NormalizedPositioningObservation(
            **{**payload, "received_at": MOMENT.replace(tzinfo=None)}
        )


def test_identity_and_fingerprint_are_deterministic_and_ignore_receipt_time() -> None:
    first = _observation(received_at=MOMENT)
    retry = _observation(received_at=MOMENT + timedelta(days=10))
    assert first.fingerprint == retry.fingerprint
    assert first.observation_id == retry.observation_id
    assert first.series_key.series_id == _key().series_id


def test_series_identity_isolates_source_instrument_venue_participant_measure_frequency_and_period() -> None:
    base = _key()
    variants = (
        _key(source_id="other-source"),
        _key(instrument_id="SILVER"),
        _key(venue="CME"),
        _key(participant_category=ParticipantCategory.LEVERAGED_FUND),
        _key(measure=PositioningMeasure.OPEN_INTEREST),
        _key(frequency=PositioningFrequency.MONTHLY),
    )
    assert all(item.series_id != base.series_id for item in variants)
    assert _period(0).period_id != _period(1).period_id


def test_reference_period_and_revision_contracts_are_strict() -> None:
    original = _observation()
    revision = _observation(value="124000", version=2, revision_of_observation_id=original.observation_id)
    assert revision.observation_version == 2
    assert revision.revision_of_observation_id == original.observation_id
    payload = original.model_dump(round_trip=True)
    with pytest.raises(ValidationError, match="revised observations"):
        NormalizedPositioningObservation.model_validate(
            {**payload, "observation_version": 2, "revision_of_observation_id": ""}
        )
    with pytest.raises(ValidationError, match="calendar month"):
        PositioningReferencePeriod(
            kind=PositioningReferencePeriodKind.MONTH,
            start_date=date(2026, 9, 2),
            end_date=date(2026, 9, 30),
        )
    assert PositioningReferencePeriod(
        kind=PositioningReferencePeriodKind.DATE,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        label="Provider label A",
    ).period_id == PositioningReferencePeriod(
        kind=PositioningReferencePeriodKind.DATE,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        label="Provider label B",
    ).period_id


def test_store_selects_current_version_preserves_history_and_nested_evidence_is_immutable() -> None:
    store = PositioningStore()
    original = _observation(value="125000")
    revision = _observation(value="124000", version=2, revision_of_observation_id=original.observation_id)
    store.ingest(original)
    store.ingest(revision)
    series = store.snapshot().series[0]
    assert len(series.observations) == 2
    assert len(series.current_observations) == 1
    assert series.current_observations[0].value.value == Decimal("124000")
    history = store.vintage_history(original.series_key, original.reference_period)
    assert tuple(item.observation_version for item in history) == (1, 2)
    with pytest.raises(FrozenInstanceError):
        history[0].value = EconomicValue(value=Decimal("1"), unit=EconomicValueUnit.COUNT)  # type: ignore[misc]


def test_store_collapses_exact_duplicates_and_rejects_same_version_conflicts() -> None:
    store = PositioningStore()
    original = _observation()
    store.ingest(original)
    store.ingest(_observation(received_at=MOMENT + timedelta(days=1)))
    assert store.observation_count == 1
    with pytest.raises(ValueError, match="conflicting positioning-observation"):
        store.ingest(_observation(value="999999"))


def test_post_eviction_conflicts_and_bounded_metadata_are_safe_and_deterministic() -> None:
    store = PositioningStore(
        max_series=1,
        max_occurrences_per_series=1,
        max_vintages_per_occurrence=1,
        max_version_identities=2,
    )
    original = _observation(0)
    store.ingest(original)
    store.ingest(_observation(1))
    assert store.observation_count == 1
    store.ingest(_observation(0, received_at=MOMENT + timedelta(days=5)))
    with pytest.raises(ValueError, match="conflicting positioning-observation"):
        store.ingest(_observation(0, value="999999"))
    assert store.version_identity_count <= 2


def test_source_occurrence_and_version_variants_are_isolated_after_eviction() -> None:
    store = PositioningStore(
        max_series=4,
        max_occurrences_per_series=1,
        max_vintages_per_occurrence=2,
        max_version_identities=8,
    )
    original = _observation(0)
    store.ingest(original)
    store.ingest(_observation(1))
    store.ingest(_observation(0, received_at=MOMENT + timedelta(days=5)))
    store.ingest(
        _observation(
            0,
            value="124000",
            version=2,
            revision_of_observation_id=original.observation_id,
        )
    )
    other_source = PositioningSource(
        source_id="other-positioning-source",
        display_name="Other Positioning Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.POSITIONING_DATA,
    )
    store.ingest(
        _observation(
            0,
            source=other_source,
            key=_key(source_id=other_source.source_id),
            value="999999",
        )
    )
    assert store.series_count == 2


def test_bounded_retention_and_snapshots_are_insertion_order_independent_and_thread_safe() -> None:
    observations = tuple(_observation(index) for index in range(12))
    first = PositioningStore(max_occurrences_per_series=3)
    second = PositioningStore(max_occurrences_per_series=3)
    for observation in observations:
        first.ingest(observation)
    with ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(second.ingest, reversed(observations)))
    assert first.snapshot() == second.snapshot()
    assert first.observation_count <= 3
    frozen_snapshot = first.snapshot()
    second.ingest(_observation(20))
    assert frozen_snapshot != second.snapshot()
    assert len(frozen_snapshot.series[0].observations) <= 3


def test_symbol_scoping_has_no_heuristic_instrument_aliases() -> None:
    store = PositioningStore()
    store.ingest(_observation(key=_key(symbol="XAU/USD")))
    store.ingest(_observation(key=_key(source_series_id="GOLD-FUTURES", symbol="GOLD")))
    assert len(store.snapshot(symbol="XAUUSD").series) == 1
    assert len(store.snapshot(symbol="GOLD").series) == 1


def test_router_gives_values_only_to_capitol_and_provenance_only_to_ledger() -> None:
    store = EvidenceStore()
    store.ingest_positioning_observation(_observation())
    snapshot = store.snapshot(symbol="XAUUSD")
    router = EvidenceRouter()
    capitol = router.route(agent_id="capitol_smart_money", snapshot=snapshot)
    assert len(capitol.positioning_series) == 1
    assert len(capitol.positioning_series[0].observations) == 1
    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert ledger.positioning_series == ()
    assert len(ledger.provenance) == 1
    for agent_id in (
        "atlas_macro",
        "scout_recon",
        "athena_analyst",
        "chartist_technician",
        "oracle_quant",
        "sentinel_risk_officer",
        "pilot_execution",
    ):
        routed = router.route(agent_id=agent_id, snapshot=snapshot)
        assert routed.positioning_series == ()


def test_router_caps_positioning_observations_before_specialist_artifact_limits() -> None:
    store = EvidenceStore(
        max_positioning_occurrences_per_series=150,
        max_positioning_vintages_per_occurrence=1,
    )
    for index in range(120):
        store.ingest_positioning_observation(_observation(index))
    routed = EvidenceRouter().route(
        agent_id="capitol_smart_money",
        snapshot=store.snapshot(symbol="XAUUSD"),
    )
    assert sum(len(series.observations) for series in routed.positioning_series) == 100


def test_one_snapshot_is_reused_and_capitol_artifact_is_factual_and_non_directional() -> None:
    class CountingStore(EvidenceStore):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_calls = 0

        def snapshot(self, *, symbol: str | None = None):  # type: ignore[override]
            self.snapshot_calls += 1
            return super().snapshot(symbol=symbol)

    class RecordingRouter(EvidenceRouter):
        def __init__(self) -> None:
            self.snapshot_ids: list[int] = []

        def route(self, *, agent_id: str, snapshot):  # type: ignore[override]
            self.snapshot_ids.append(id(snapshot))
            return super().route(agent_id=agent_id, snapshot=snapshot)

    store = CountingStore()
    store.ingest_positioning_observation(_observation())
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
        evidence_router=router,
    )
    snapshot = store.snapshot(symbol="XAUUSD")
    run = core._analyze_run(  # noqa: SLF001 - validates internal evidence flow.
        AnalyzeRequest(symbol="XAUUSD", query="institutional positioning review"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    capitol = artifacts["capitol_smart_money"]
    assert len(capitol.positioning_observations) == 1
    assert capitol.stance is SpecialistStance.INSUFFICIENT_DATA
    assert capitol.confidence == 0.0
    assert not hasattr(capitol.positioning_observations[0], "direction")
    assert artifacts["ledger_the_book"].positioning_observations == ()
    assert store.snapshot_calls == 1
    assert len(router.snapshot_ids) == 9
    assert len(set(router.snapshot_ids)) == 1


def test_positioning_cannot_satisfy_sentinel_or_enable_pilot_and_public_analyze_is_unchanged() -> None:
    store = EvidenceStore()
    store.ingest_positioning_observation(_observation())
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    request = AnalyzeRequest(symbol="XAUUSD", query="positioning execution review")
    response = core.analyze(request)
    workflow = core.run_trade_proposal_workflow(
        request
    )
    assert set(response.model_dump()) == {
        "analysis_id",
        "mode",
        "symbol",
        "query",
        "selected_agents",
        "routing",
        "agent_results",
        "synthesis",
    }
    assert workflow.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    assert workflow.proposal.parameters.direction is None
    assert workflow.proposal.parameters.reference_price is None
    assert not workflow.proposal.observed_market_reference
    pilot = next(item for item in workflow.analysis.agent_results if item.agent_id == "pilot_execution")
    assert pilot.status == "completed"
    assert "disabled" in pilot.summary.lower()


def test_positioning_ingestion_service_uses_only_the_provider_neutral_boundary() -> None:
    store = EvidenceStore()

    class Consumer:
        def ingest_positioning_observation(
            self,
            observation: NormalizedPositioningObservation,
        ) -> object:
            return store.ingest_positioning_observation(observation)

    service = PositioningIngestionService(consumer=Consumer())
    result = service.ingest(_observation())
    assert result == store.snapshot(symbol="XAUUSD").positioning_series[0]
    with pytest.raises(TypeError, match="NormalizedPositioningObservation"):
        service.ingest(object())  # type: ignore[arg-type]
