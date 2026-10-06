"""Phase 5B-6 tests for provider-neutral analyst-estimate evidence."""

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
from app.evidence import EvidenceRouter, EvidenceStore, EstimateStore
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.estimates import (
    EstimateFrequency,
    EstimateReferencePeriod,
    EstimateReferencePeriodKind,
    EstimateSeriesKey,
    EstimateSource,
    EstimateStatistic,
    NormalizedEstimateObservation,
)
from app.models.events import DataDomain, SourceTransport
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistOperationalState, SpecialistStance
from app.services.estimates import EstimateIngestionService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = EstimateSource(
    source_id="synthetic-estimate-source",
    display_name="Synthetic Estimate Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.FUNDAMENTAL_DATA,
)


def _key(
    *,
    source_id: str = SOURCE.source_id,
    source_series_id: str = "ACME-REVENUE-CONSENSUS",
    issuer_id: str = "ACME-ISSUER-001",
    symbol: str | None = "ACME",
    venue: str | None = "NYSE",
    metric_id: str = "revenue",
    statistic: EstimateStatistic = EstimateStatistic.CONSENSUS_MEAN,
    frequency: EstimateFrequency = EstimateFrequency.ANNUAL,
    currency: str | None = "USD",
    unit: EconomicValueUnit = EconomicValueUnit.CURRENCY,
    scale: EconomicValueScale = EconomicValueScale.MILLION,
) -> EstimateSeriesKey:
    return EstimateSeriesKey(
        source_id=source_id,
        source_series_id=source_series_id,
        issuer_id=issuer_id,
        symbol=symbol,
        venue=venue,
        metric_id=metric_id,
        statistic=statistic,
        frequency=frequency,
        currency=currency,
        value_unit=unit,
        value_scale=scale,
    )


def _period(
    index: int = 0,
    *,
    kind: EstimateReferencePeriodKind = EstimateReferencePeriodKind.INTERVAL,
) -> EstimateReferencePeriod:
    start = date(2027 + index, 1, 1)
    if kind is EstimateReferencePeriodKind.POINT_IN_TIME:
        point = date(2027, 12, 31) + timedelta(days=index)
        return EstimateReferencePeriod(kind=kind, start_date=point, end_date=point, label="FY")
    return EstimateReferencePeriod(
        kind=kind,
        start_date=start,
        end_date=date(2027 + index, 12, 31),
        label=f"FY{2027 + index}",
    )


def _observation(
    index: int = 0,
    *,
    key: EstimateSeriesKey | None = None,
    source: EstimateSource = SOURCE,
    period: EstimateReferencePeriod | None = None,
    value: str = "1250",
    version: int = 1,
    revision_of_observation_id: str | None = None,
    as_of: datetime | None = None,
    received_at: datetime | None = None,
    published_at: datetime | None = None,
    vintage_at: datetime | None = None,
) -> NormalizedEstimateObservation:
    actual_key = key or _key(source_id=source.source_id)
    reference = period or _period(index)
    initial = NormalizedEstimateObservation(
        source=source,
        series_key=actual_key,
        reference_period=reference,
        value=EconomicValue(
            value=Decimal(value),
            unit=actual_key.value_unit,
            scale=actual_key.value_scale,
        ),
        as_of=as_of or MOMENT + timedelta(days=index),
        observation_version=1,
        received_at=received_at or MOMENT + timedelta(days=index, minutes=1),
        published_at=published_at,
        vintage_at=vintage_at,
    )
    if version == 1:
        return initial
    payload = initial.model_dump(round_trip=True)
    return NormalizedEstimateObservation.model_validate(
        {
            **payload,
            "observation_version": version,
            "revision_of_observation_id": revision_of_observation_id or initial.observation_id,
        }
    )


def test_contracts_are_frozen_nested_and_reject_raw_payloads() -> None:
    observation = _observation()
    with pytest.raises(ValidationError):
        observation.as_of = MOMENT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        observation.value.value = Decimal("2")  # type: ignore[misc]
    with pytest.raises(ValidationError, match="raw_payload"):
        NormalizedEstimateObservation.model_validate(
            {**observation.model_dump(round_trip=True), "raw_payload": {"provider": "unsafe"}}
        )

    evidence = EstimateStore().ingest(observation)
    assert evidence is not None
    with pytest.raises(FrozenInstanceError):
        evidence.observations = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        evidence.observations.append(evidence.observations[0])  # type: ignore[attr-defined]


def test_value_and_time_contracts_preserve_distinct_target_and_vintage_semantics() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    observation = _observation(
        as_of=datetime(2026, 10, 6, 17, 30, tzinfo=offset),
        published_at=datetime(2026, 10, 6, 18, 30, tzinfo=offset),
        vintage_at=datetime(2026, 10, 6, 19, 30, tzinfo=offset),
    )
    assert observation.reference_period.end_date == date(2027, 12, 31)
    assert observation.as_of == MOMENT
    assert observation.published_at == MOMENT + timedelta(hours=1)
    assert observation.vintage_at == MOMENT + timedelta(hours=2)
    with pytest.raises(ValidationError, match="finite"):
        EconomicValue(value=Decimal("NaN"), unit=EconomicValueUnit.CURRENCY)
    with pytest.raises(ValidationError, match="timezone"):
        _observation(as_of=datetime(2026, 10, 6, 12, 0))
    with pytest.raises(ValidationError, match="matching start and end"):
        EstimateReferencePeriod(
            kind=EstimateReferencePeriodKind.POINT_IN_TIME,
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 2),
        )


def test_identity_fingerprints_ignore_receipt_time_and_isolate_dimensions() -> None:
    first = _observation(received_at=MOMENT)
    second = _observation(received_at=MOMENT + timedelta(days=3))
    assert first.fingerprint == second.fingerprint
    assert first.observation_id == second.observation_id
    variants = (
        _key(source_series_id="other"),
        _key(issuer_id="OTHER"),
        _key(symbol="OTHER"),
        _key(venue="NASDAQ"),
        _key(metric_id="eps"),
        _key(statistic=EstimateStatistic.MEDIAN),
        _key(frequency=EstimateFrequency.QUARTERLY),
    )
    assert len({first.series_key.series_id, *(item.series_id for item in variants)}) == 8
    assert _observation(period=_period(1)).observation_id != first.observation_id
    assert EstimateReferencePeriod(
        kind=EstimateReferencePeriodKind.INTERVAL,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 12, 31),
        label="different display label",
    ).period_id == _period().period_id


def test_source_unit_and_revision_contracts_are_strict_and_immutable() -> None:
    with pytest.raises(ValidationError, match="fundamental_data"):
        EstimateSource(
            source_id="wrong",
            display_name="Wrong",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        )
    with pytest.raises(ValidationError, match="count value_unit"):
        _key(statistic=EstimateStatistic.ANALYST_COUNT)
    original = _observation()
    revised = _observation(version=2, value="1300", revision_of_observation_id=original.observation_id)
    assert original.value.value == Decimal("1250")
    assert revised.revision_of_observation_id == original.observation_id
    with pytest.raises(ValidationError, match="version-one"):
        NormalizedEstimateObservation.model_validate(
            {
                **original.model_dump(round_trip=True),
                "revision_of_observation_id": original.observation_id,
            }
        )
    with pytest.raises(ValidationError, match="require revision"):
        NormalizedEstimateObservation.model_validate(
            {**original.model_dump(round_trip=True), "observation_version": 2}
        )


def test_store_handles_duplicates_conflicts_eviction_and_bounded_metadata() -> None:
    store = EstimateStore(
        max_series=1,
        max_occurrences_per_series=1,
        max_vintages_per_occurrence=1,
        max_version_identities=2,
    )
    first = _observation(0)
    assert store.ingest(first) is not None
    assert store.ingest(first) is not None
    assert store.observation_count == 1
    assert store.ingest(_observation(1)) is not None  # evicts target period zero
    assert store.observation_count == 1
    assert store.ingest(first) is None  # exact duplicate remains idempotent after eviction
    with pytest.raises(ValueError, match="conflicting estimate-observation"):
        store.ingest(_observation(0, value="999"))
    assert store.ingest(_observation(2)) is not None
    assert store.version_identity_count <= 2

    source_two = EstimateSource(
        source_id="other-estimate-source",
        display_name="Other Estimate Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.FUNDAMENTAL_DATA,
    )
    permissive = EstimateStore()
    assert permissive.ingest(first) is not None
    assert permissive.ingest(_observation(1)) is not None
    assert permissive.ingest(_observation(version=2, value="1300")) is not None
    assert permissive.ingest(
        _observation(source=source_two, key=_key(source_id=source_two.source_id))
    ) is not None


def test_store_retention_snapshot_isolation_ordering_and_concurrent_ingestion() -> None:
    first = EstimateStore(max_occurrences_per_series=2)
    second = EstimateStore(max_occurrences_per_series=2)
    observations = (_observation(0), _observation(1), _observation(2))
    for item in observations:
        first.ingest(item)
    for item in reversed(observations):
        second.ingest(item)
    assert first.snapshot() == second.snapshot()
    snapshot = first.snapshot(symbol="ACME")
    first.ingest(_observation(3))
    assert len(snapshot.series[0].observations) == 2
    assert snapshot.fingerprint != first.snapshot().fingerprint

    concurrent = EstimateStore(max_occurrences_per_series=20)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(concurrent.ingest, tuple(_observation(index) for index in range(12))))
    assert concurrent.observation_count == 12
    assert concurrent.snapshot().fingerprint == concurrent.snapshot().fingerprint


def test_domain_separation_routing_caps_and_ledger_provenance_only() -> None:
    store = EvidenceStore()
    store.ingest_estimate_observation(_observation())
    snapshot = store.snapshot(symbol="ACME")
    assert snapshot.fundamental_series == ()
    assert len(snapshot.estimate_series) == 1
    router = EvidenceRouter()
    athena = router.route(agent_id="athena_analyst", snapshot=snapshot)
    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert len(athena.estimate_series) == 1
    assert ledger.estimate_series == ()
    assert ledger.provenance[-1].event_id == _observation().observation_id
    for agent_id in (
        "atlas_macro",
        "capitol_smart_money",
        "scout_recon",
        "chartist_technician",
        "oracle_quant",
        "sentinel_risk_officer",
        "pilot_execution",
    ):
        assert router.route(agent_id=agent_id, snapshot=snapshot).estimate_series == ()

    capped = EvidenceStore(max_estimate_occurrences_per_series=150)
    for index in range(101):
        capped.ingest_estimate_observation(_observation(index))
    routed = router.route(agent_id="athena_analyst", snapshot=capped.snapshot(symbol="ACME"))
    assert sum(len(series.observations) for series in routed.estimate_series) == 100


def test_one_snapshot_artifact_master_traceability_and_safety_boundaries() -> None:
    class RecordingRouter(EvidenceRouter):
        def __init__(self) -> None:
            self.snapshot_ids: list[int] = []

        def route(self, *, agent_id: str, snapshot):  # type: ignore[no-untyped-def]
            self.snapshot_ids.append(id(snapshot))
            return super().route(agent_id=agent_id, snapshot=snapshot)

    store = EvidenceStore()
    store.ingest_estimate_observation(_observation())
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
        evidence_router=router,
    )
    snapshot = store.snapshot(symbol="ACME")
    run = core._analyze_run(  # noqa: SLF001 - verifies the internal snapshot boundary
        AnalyzeRequest(symbol="ACME", query="Review fundamental earnings"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    assert set(router.snapshot_ids) == {id(snapshot)}
    athena = next(item for item in run.specialist_analyses if item.agent_id == "athena_analyst")
    pilot = next(item for item in run.specialist_analyses if item.agent_id == "pilot_execution")
    assert len(athena.estimate_observations) == 1
    assert athena.stance is SpecialistStance.INSUFFICIENT_DATA
    assert pilot.estimate_observations == ()
    assert pilot.operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert build_evidence_bundle(snapshot).references[-1].event_id == _observation().observation_id

    workflow = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="ACME", query="Review fundamental earnings")
    )
    assert workflow.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    response = core.analyze(AnalyzeRequest(symbol="ACME", query="Review fundamental earnings"))
    assert set(response.model_dump()) == {
        "analysis_id", "mode", "symbol", "query", "selected_agents", "routing", "agent_results", "synthesis"
    }


def test_ingestion_boundary_is_provider_neutral_without_a_real_connector() -> None:
    class Consumer:
        def __init__(self) -> None:
            self.store = EstimateStore()

        def ingest_estimate_observation(self, observation: NormalizedEstimateObservation):  # type: ignore[no-untyped-def]
            return self.store.ingest(observation)

    consumer = Consumer()
    service = EstimateIngestionService(consumer=consumer)
    result = service.ingest(_observation())
    assert result == consumer.store.snapshot(symbol="ACME").series[0]
    with pytest.raises(TypeError, match="NormalizedEstimateObservation"):
        service.ingest(object())  # type: ignore[arg-type]
