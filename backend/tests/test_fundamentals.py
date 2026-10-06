"""Phase 5B-5 tests for provider-neutral financial-statement evidence."""

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
from app.evidence import EvidenceRouter, EvidenceStore, FundamentalStore
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.fundamentals import (
    FinancialStatementKind,
    FundamentalFrequency,
    FundamentalReferencePeriod,
    FundamentalReferencePeriodKind,
    FundamentalReportingScope,
    FundamentalSeriesKey,
    FundamentalSource,
    NormalizedFundamentalObservation,
)
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistStance
from app.services.fundamentals import FundamentalIngestionService


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = FundamentalSource(
    source_id="synthetic-fundamental-source",
    display_name="Synthetic Fundamental Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.FUNDAMENTAL_DATA,
)


def _key(
    *,
    source_id: str = SOURCE.source_id,
    source_series_id: str = "ACME-REVENUE",
    issuer_id: str = "ACME-ISSUER-001",
    symbol: str | None = "ACME",
    venue: str | None = "NYSE",
    statement_kind: FinancialStatementKind = FinancialStatementKind.INCOME_STATEMENT,
    metric_id: str = "revenue",
    frequency: FundamentalFrequency = FundamentalFrequency.ANNUAL,
    reporting_scope: FundamentalReportingScope = FundamentalReportingScope.CONSOLIDATED,
    currency: str | None = "USD",
    unit: EconomicValueUnit = EconomicValueUnit.CURRENCY,
    scale: EconomicValueScale = EconomicValueScale.MILLION,
) -> FundamentalSeriesKey:
    return FundamentalSeriesKey(
        source_id=source_id,
        source_series_id=source_series_id,
        issuer_id=issuer_id,
        symbol=symbol,
        venue=venue,
        statement_kind=statement_kind,
        metric_id=metric_id,
        frequency=frequency,
        reporting_scope=reporting_scope,
        currency=currency,
        value_unit=unit,
        value_scale=scale,
    )


def _period(
    index: int = 0,
    *,
    kind: FundamentalReferencePeriodKind = FundamentalReferencePeriodKind.INTERVAL,
) -> FundamentalReferencePeriod:
    start = date(2020 + index, 1, 1)
    if kind is FundamentalReferencePeriodKind.POINT_IN_TIME:
        return FundamentalReferencePeriod(
            kind=kind,
            start_date=date(2020, 12, 31) + timedelta(days=index),
            end_date=date(2020, 12, 31) + timedelta(days=index),
            label=f"FY{2020 + index}",
        )
    return FundamentalReferencePeriod(
        kind=kind,
        start_date=start,
        end_date=date(2020 + index, 12, 31),
        label=f"FY{2020 + index}",
    )


def _observation(
    index: int = 0,
    *,
    key: FundamentalSeriesKey | None = None,
    source: FundamentalSource = SOURCE,
    period: FundamentalReferencePeriod | None = None,
    value: str = "1250",
    version: int = 1,
    revision_of_observation_id: str | None = None,
    received_at: datetime | None = None,
    reported_at: datetime | None = None,
    filed_at: datetime | None = None,
    published_at: datetime | None = None,
    vintage_at: datetime | None = None,
) -> NormalizedFundamentalObservation:
    actual_key = key or _key(source_id=source.source_id)
    reference = period or _period(index)
    initial = NormalizedFundamentalObservation(
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
        reported_at=reported_at,
        filed_at=filed_at,
        published_at=published_at,
        vintage_at=vintage_at,
    )
    if version == 1:
        return initial
    payload = initial.model_dump(round_trip=True)
    return NormalizedFundamentalObservation.model_validate(
        {
            **payload,
            "observation_version": version,
            "revision_of_observation_id": revision_of_observation_id or initial.observation_id,
        }
    )


def test_contracts_are_frozen_nested_and_exclude_raw_payloads() -> None:
    observation = _observation()
    with pytest.raises(ValidationError):
        observation.received_at = MOMENT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        observation.series_key.issuer_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        observation.reference_period.label = "changed"  # type: ignore[misc]
    payload = observation.model_dump(exclude={"observation_id", "fingerprint"})
    with pytest.raises(ValidationError):
        NormalizedFundamentalObservation(**payload, raw_payload={"forbidden": True})
    with pytest.raises(ValidationError, match="finite"):
        EconomicValue(value=Decimal("NaN"), unit=EconomicValueUnit.CURRENCY)


def test_typed_value_time_semantics_and_utc_normalization_are_preserved() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    observation = _observation(
        reported_at=MOMENT.astimezone(offset),
        filed_at=(MOMENT + timedelta(hours=1)).astimezone(offset),
        published_at=(MOMENT + timedelta(hours=2)).astimezone(offset),
        vintage_at=(MOMENT + timedelta(hours=3)).astimezone(offset),
        received_at=MOMENT + timedelta(hours=4),
    )
    assert observation.reported_at == MOMENT
    assert observation.filed_at == MOMENT + timedelta(hours=1)
    assert observation.published_at == MOMENT + timedelta(hours=2)
    assert observation.vintage_at == MOMENT + timedelta(hours=3)
    assert observation.value == EconomicValue(
        value=Decimal("1250"),
        unit=EconomicValueUnit.CURRENCY,
        scale=EconomicValueScale.MILLION,
    )
    payload = observation.model_dump(exclude={"observation_id", "fingerprint"})
    with pytest.raises(ValidationError, match="timezone"):
        NormalizedFundamentalObservation(
            **{**payload, "received_at": MOMENT.replace(tzinfo=None)}
        )


def test_period_identity_is_semantic_and_contract_is_strict() -> None:
    first = FundamentalReferencePeriod(
        kind=FundamentalReferencePeriodKind.INTERVAL,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        label="FY 2025 provider A",
    )
    second = FundamentalReferencePeriod(
        kind=FundamentalReferencePeriodKind.INTERVAL,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        label="2025 Annual provider B",
    )
    assert first.period_id == second.period_id
    with pytest.raises(ValidationError, match="positive duration"):
        FundamentalReferencePeriod(
            kind=FundamentalReferencePeriodKind.INTERVAL,
            start_date=date(2025, 12, 31),
            end_date=date(2025, 12, 31),
        )
    with pytest.raises(ValidationError, match="matching start and end"):
        FundamentalReferencePeriod(
            kind=FundamentalReferencePeriodKind.POINT_IN_TIME,
            start_date=date(2025, 12, 30),
            end_date=date(2025, 12, 31),
        )


def test_identity_fingerprints_ignore_receipt_time_and_isolate_all_key_dimensions() -> None:
    first = _observation(received_at=MOMENT)
    retry = _observation(received_at=MOMENT + timedelta(days=20))
    assert first.fingerprint == retry.fingerprint
    assert first.observation_id == retry.observation_id

    base = _key()
    variants = (
        _key(source_id="other-source"),
        _key(source_series_id="ACME-NET-INCOME"),
        _key(issuer_id="ACME-ISSUER-002"),
        _key(metric_id="net_income"),
        _key(statement_kind=FinancialStatementKind.BALANCE_SHEET),
        _key(reporting_scope=FundamentalReportingScope.STANDALONE),
        _key(currency="EUR"),
        _key(venue="NASDAQ"),
        _key(frequency=FundamentalFrequency.QUARTERLY),
    )
    assert all(item.series_id != base.series_id for item in variants)
    assert _period(0).period_id != _period(1).period_id


def test_source_and_currency_contracts_are_strict() -> None:
    with pytest.raises(ValidationError, match="fundamental_data"):
        FundamentalSource(
            source_id="invalid",
            display_name="Invalid",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.ECONOMIC_DATA,
        )
    with pytest.raises(ValidationError, match="require currency"):
        _key(currency=None)
    with pytest.raises(ValidationError, match="metric_id"):
        _key(metric_id="Revenue Growth")


def test_no_heuristic_issuer_aliases_are_introduced() -> None:
    store = FundamentalStore()
    store.ingest(_observation(key=_key(issuer_id="ISSUER-A", symbol="ACME")))
    store.ingest(
        _observation(
            key=_key(
                source_series_id="ACME-REVENUE-B",
                issuer_id="ISSUER-B",
                symbol="ACMEHOLDINGS",
            )
        )
    )
    assert len(store.snapshot(symbol="ACME").series) == 1
    assert len(store.snapshot(symbol="ACMEHOLDINGS").series) == 1
    assert len(store.snapshot(symbol="ACME-HOLDINGS").series) == 1


def test_version_rules_and_revision_history_are_immutable() -> None:
    original = _observation()
    revision = _observation(
        value="1300",
        version=2,
        revision_of_observation_id=original.observation_id,
    )
    assert revision.observation_version == 2
    payload = original.model_dump(round_trip=True)
    with pytest.raises(ValidationError, match="revised observations"):
        NormalizedFundamentalObservation.model_validate(
            {**payload, "observation_version": 2, "revision_of_observation_id": ""}
        )
    with pytest.raises(ValidationError, match="version-one"):
        NormalizedFundamentalObservation.model_validate(
            {**payload, "revision_of_observation_id": original.observation_id}
        )

    store = FundamentalStore()
    store.ingest(original)
    store.ingest(revision)
    history = store.vintage_history(original.series_key, original.reference_period)
    assert tuple(item.observation_version for item in history) == (1, 2)
    assert store.current_observations(original.series_key)[0].value.value == Decimal("1300")
    with pytest.raises(FrozenInstanceError):
        history[0].value = EconomicValue(value=Decimal("1"), unit=EconomicValueUnit.CURRENCY)  # type: ignore[misc]


def test_duplicates_conflicts_post_eviction_and_bounded_metadata_are_safe() -> None:
    store = FundamentalStore(
        max_series=1,
        max_occurrences_per_series=1,
        max_vintages_per_occurrence=1,
        max_version_identities=2,
    )
    original = _observation(0)
    store.ingest(original)
    store.ingest(_observation(0, received_at=MOMENT + timedelta(days=20)))
    assert store.observation_count == 1
    store.ingest(_observation(1))
    assert store.observation_count == 1
    store.ingest(_observation(0, received_at=MOMENT + timedelta(days=21)))
    with pytest.raises(ValueError, match="conflicting fundamental-observation"):
        store.ingest(_observation(0, value="9999"))
    store.ingest(
        _observation(
            0,
            value="1300",
            version=2,
            revision_of_observation_id=original.observation_id,
        )
    )
    other_source = FundamentalSource(
        source_id="other-fundamental-source",
        display_name="Other Fundamental Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.FUNDAMENTAL_DATA,
    )
    store.ingest(
        _observation(
            0,
            source=other_source,
            key=_key(source_id=other_source.source_id),
            value="9999",
        )
    )
    assert store.version_identity_count <= 2


def test_conflicting_content_is_rejected_after_the_entire_series_is_evicted() -> None:
    store = FundamentalStore(
        max_series=1,
        max_occurrences_per_series=1,
        max_vintages_per_occurrence=1,
        max_version_identities=2,
    )
    retained_key, evicted_key = sorted(
        (
            _key(source_series_id="series-a"),
            _key(source_series_id="series-b"),
        ),
        key=lambda item: item.series_id,
    )
    original = _observation(key=evicted_key)
    replacement = _observation(index=1, key=retained_key)
    store.ingest(original)
    store.ingest(replacement)
    assert store.snapshot().series[0].series_id == retained_key.series_id
    assert store.observation_count == 1

    with pytest.raises(ValueError, match="conflicting fundamental-observation"):
        store.ingest(_observation(key=evicted_key, value="9999"))
    assert store.version_identity_count <= 2


def test_retention_snapshot_isolation_and_concurrent_ingestion_are_deterministic() -> None:
    observations = tuple(_observation(index) for index in range(12))
    first = FundamentalStore(max_occurrences_per_series=3)
    second = FundamentalStore(max_occurrences_per_series=3)
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


def test_router_gives_values_only_to_athena_and_provenance_only_to_ledger() -> None:
    store = EvidenceStore()
    store.ingest_fundamental_observation(_observation())
    snapshot = store.snapshot(symbol="ACME")
    router = EvidenceRouter()
    athena = router.route(agent_id="athena_analyst", snapshot=snapshot)
    assert len(athena.fundamental_series) == 1
    assert len(athena.fundamental_series[0].observations) == 1
    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert ledger.fundamental_series == ()
    assert len(ledger.provenance) == 1
    for agent_id in (
        "atlas_macro",
        "capitol_smart_money",
        "scout_recon",
        "chartist_technician",
        "oracle_quant",
        "sentinel_risk_officer",
        "pilot_execution",
    ):
        assert router.route(agent_id=agent_id, snapshot=snapshot).fundamental_series == ()


def test_router_cap_is_deterministic_and_below_specialist_artifact_limit() -> None:
    store = EvidenceStore(
        max_fundamental_occurrences_per_series=150,
        max_fundamental_vintages_per_occurrence=1,
    )
    for index in range(120):
        store.ingest_fundamental_observation(_observation(index))
    routed = EvidenceRouter().route(
        agent_id="athena_analyst",
        snapshot=store.snapshot(symbol="ACME"),
    )
    assert sum(len(series.observations) for series in routed.fundamental_series) == 100


def test_one_snapshot_is_reused_and_athena_artifact_is_factual_and_non_directional() -> None:
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
    store.ingest_fundamental_observation(_observation())
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
        evidence_router=router,
    )
    snapshot = store.snapshot(symbol="ACME")
    run = core._analyze_run(  # noqa: SLF001 - validates internal evidence flow.
        AnalyzeRequest(symbol="ACME", query="fundamental execution review"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    athena = artifacts["athena_analyst"]
    assert len(athena.fundamental_observations) == 1
    assert athena.stance is SpecialistStance.INSUFFICIENT_DATA
    assert athena.confidence == 0.0
    assert not hasattr(athena.fundamental_observations[0], "direction")
    assert artifacts["ledger_the_book"].fundamental_observations == ()
    assert store.snapshot_calls == 1
    assert len(router.snapshot_ids) == 9
    assert len(set(router.snapshot_ids)) == 1


def test_fundamentals_cannot_satisfy_sentinel_or_enable_pilot_and_public_analyze_is_unchanged() -> None:
    store = EvidenceStore()
    store.ingest_fundamental_observation(_observation())
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    request = AnalyzeRequest(symbol="ACME", query="fundamental execution review")
    response = core.analyze(request)
    workflow = core.run_trade_proposal_workflow(request)
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


def test_fundamental_ingestion_service_uses_only_the_provider_neutral_boundary() -> None:
    store = EvidenceStore()

    class Consumer:
        def ingest_fundamental_observation(
            self,
            observation: NormalizedFundamentalObservation,
        ) -> object:
            return store.ingest_fundamental_observation(observation)

    service = FundamentalIngestionService(consumer=Consumer())
    result = service.ingest(_observation())
    assert result == store.snapshot(symbol="ACME").fundamental_series[0]
    with pytest.raises(TypeError, match="NormalizedFundamentalObservation"):
        service.ingest(object())  # type: ignore[arg-type]
