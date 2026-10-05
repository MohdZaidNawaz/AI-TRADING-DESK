"""Phase 5B-3 tests for provider-neutral persistent macro-series evidence."""

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
from app.evidence import EvidenceRouter, EvidenceStore, MacroSeriesStore
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.macro_series import (
    MacroFrequency,
    MacroReferencePeriod,
    MacroReferencePeriodKind,
    MacroSeriesKey,
    MacroSeriesSource,
    NormalizedMacroObservation,
    SeasonalAdjustment,
)
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistOperationalState, SpecialistStance
from app.services.macro_series import MacroSeriesIngestionService


MOMENT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SOURCE = MacroSeriesSource(
    source_id="synthetic-macro-source",
    display_name="Synthetic Macro Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.ECONOMIC_DATA,
)


def _key(
    *,
    source_id: str = SOURCE.source_id,
    source_series_id: str = "US10Y",
    indicator_id: str = "us_nominal_treasury_yield",
    country: str | None = "US",
    region: str | None = "North America",
    currency: str | None = "USD",
    maturity: str | None = "10Y",
    frequency: MacroFrequency = MacroFrequency.DAILY,
    seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.NOT_APPLICABLE,
    unit: EconomicValueUnit = EconomicValueUnit.PERCENT,
    scale: EconomicValueScale = EconomicValueScale.ONE,
) -> MacroSeriesKey:
    return MacroSeriesKey(
        source_id=source_id,
        source_series_id=source_series_id,
        indicator_id=indicator_id,
        country=country,
        region=region,
        currency=currency,
        maturity=maturity,
        frequency=frequency,
        seasonal_adjustment=seasonal_adjustment,
        value_unit=unit,
        value_scale=scale,
    )


def _period(
    index: int = 0,
    *,
    kind: MacroReferencePeriodKind = MacroReferencePeriodKind.DATE,
) -> MacroReferencePeriod:
    day = date(2026, 9, 1) + timedelta(days=index)
    if kind is MacroReferencePeriodKind.MONTH:
        return MacroReferencePeriod(
            kind=kind,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            label="2026-09",
        )
    if kind is MacroReferencePeriodKind.QUARTER:
        return MacroReferencePeriod(
            kind=kind,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 9, 30),
            label="2026-Q3",
        )
    if kind is MacroReferencePeriodKind.WEEK:
        return MacroReferencePeriod(kind=kind, start_date=day, end_date=day + timedelta(days=6))
    return MacroReferencePeriod(kind=kind, start_date=day, end_date=day)


def _observation(
    index: int = 0,
    *,
    key: MacroSeriesKey | None = None,
    source: MacroSeriesSource = SOURCE,
    period: MacroReferencePeriod | None = None,
    value: str = "4.25",
    version: int = 1,
    revision_of_observation_id: str | None = None,
    received_at: datetime | None = None,
    observed_at: datetime | None = None,
    effective_at: datetime | None = None,
    published_at: datetime | None = None,
    vintage_at: datetime | None = None,
) -> NormalizedMacroObservation:
    actual_key = key or _key(source_id=source.source_id)
    reference = period or _period(index)
    initial = NormalizedMacroObservation(
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
        effective_at=effective_at,
        published_at=published_at,
        vintage_at=vintage_at,
    )
    if version == 1:
        return initial
    payload = initial.model_dump(round_trip=True)
    return NormalizedMacroObservation.model_validate(
        {
            **payload,
            "observation_version": version,
            "revision_of_observation_id": revision_of_observation_id or initial.observation_id,
        }
    )


def test_contract_is_frozen_decimal_backed_and_excludes_raw_payloads() -> None:
    observation = _observation()
    assert observation.value.value == Decimal("4.25")
    with pytest.raises(ValidationError):
        observation.received_at = MOMENT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        observation.series_key.indicator_id = "changed"  # type: ignore[misc]
    payload = observation.model_dump(exclude={"observation_id", "fingerprint"})
    with pytest.raises(ValidationError):
        NormalizedMacroObservation(**payload, raw_payload={"forbidden": True})
    for invalid in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValidationError, match="finite"):
            EconomicValue(value=Decimal(invalid), unit=EconomicValueUnit.PERCENT)


def test_time_semantics_are_distinct_and_utc_normalized() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    observation = _observation(
        observed_at=MOMENT.astimezone(offset),
        effective_at=(MOMENT + timedelta(hours=1)).astimezone(offset),
        published_at=(MOMENT + timedelta(hours=2)).astimezone(offset),
        vintage_at=(MOMENT + timedelta(hours=3)).astimezone(offset),
        received_at=MOMENT + timedelta(hours=4),
    )
    assert observation.observed_at == MOMENT
    assert observation.effective_at == MOMENT + timedelta(hours=1)
    assert observation.published_at == MOMENT + timedelta(hours=2)
    assert observation.vintage_at == MOMENT + timedelta(hours=3)
    assert observation.received_at == MOMENT + timedelta(hours=4)
    payload = observation.model_dump(exclude={"observation_id", "fingerprint"})
    with pytest.raises(ValidationError, match="timezone"):
        NormalizedMacroObservation(**{**payload, "received_at": MOMENT.replace(tzinfo=None)})


def test_reference_periods_model_daily_monthly_quarterly_and_policy_semantics() -> None:
    assert _period().period_id
    assert _period(kind=MacroReferencePeriodKind.MONTH).end_date == date(2026, 9, 30)
    assert _period(kind=MacroReferencePeriodKind.QUARTER).end_date == date(2026, 9, 30)
    policy = _period(kind=MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE)
    assert policy.start_date == policy.end_date
    with pytest.raises(ValidationError, match="calendar month"):
        MacroReferencePeriod(
            kind=MacroReferencePeriodKind.MONTH,
            start_date=date(2026, 9, 2),
            end_date=date(2026, 9, 30),
        )


def test_semantic_fixtures_keep_cpi_gdp_yields_and_policy_rates_distinct() -> None:
    cpi = _observation(
        key=_key(
            source_series_id="US-CPI",
            indicator_id="us_consumer_price_index",
            frequency=MacroFrequency.MONTHLY,
            unit=EconomicValueUnit.INDEX_POINT,
        ),
        period=_period(kind=MacroReferencePeriodKind.MONTH),
        value="321.7",
    )
    gdp = _observation(
        key=_key(
            source_series_id="US-GDP",
            indicator_id="us_gross_domestic_product",
            frequency=MacroFrequency.QUARTERLY,
            unit=EconomicValueUnit.CURRENCY,
            scale=EconomicValueScale.BILLION,
        ),
        period=_period(kind=MacroReferencePeriodKind.QUARTER),
        value="29400",
    )
    treasury = _observation(key=_key(maturity="2Y"), observed_at=MOMENT)
    real_yield = _observation(
        key=_key(
            source_series_id="US-REAL-10Y",
            indicator_id="us_real_treasury_yield",
            maturity="10Y",
        ),
    )
    policy = _observation(
        key=_key(
            source_series_id="US-POLICY-RATE",
            indicator_id="us_policy_rate_target",
            maturity=None,
            frequency=MacroFrequency.POLICY_RATE,
        ),
        period=_period(kind=MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE),
        effective_at=MOMENT,
    )
    assert cpi.reference_period.kind is MacroReferencePeriodKind.MONTH
    assert gdp.reference_period.kind is MacroReferencePeriodKind.QUARTER
    assert treasury.observed_at == MOMENT
    assert real_yield.series_key.maturity == "10Y"
    assert policy.series_key.frequency is MacroFrequency.POLICY_RATE
    assert policy.effective_at == MOMENT


def test_series_identity_is_deterministic_and_isolates_all_key_dimensions() -> None:
    base = _key()
    variants = (
        _key(source_id="other-source"),
        _key(source_series_id="OTHER-10Y"),
        _key(country="GB"),
        _key(frequency=MacroFrequency.WEEKLY),
        _key(maturity="2Y"),
        _key(seasonal_adjustment=SeasonalAdjustment.UNKNOWN),
        _key(unit=EconomicValueUnit.RATE),
        _key(scale=EconomicValueScale.MILLION),
    )
    assert base.series_id == _key().series_id
    assert all(item.series_id != base.series_id for item in variants)


def test_revision_contract_fingerprint_and_identity_are_deterministic() -> None:
    original = _observation(received_at=MOMENT)
    duplicate = _observation(received_at=MOMENT + timedelta(days=7))
    revision = _observation(value="4.10", version=2, revision_of_observation_id=original.observation_id)
    assert original.fingerprint == duplicate.fingerprint
    assert original.observation_id == duplicate.observation_id
    assert revision.observation_version == 2
    assert revision.revision_of_observation_id == original.observation_id
    payload = original.model_dump(round_trip=True)
    with pytest.raises(ValidationError, match="revised observations"):
        NormalizedMacroObservation.model_validate(
            {**payload, "observation_version": 2, "revision_of_observation_id": ""}
        )


def test_store_retains_current_and_immutable_vintage_history() -> None:
    store = MacroSeriesStore()
    original = _observation(value="4.25")
    revision = _observation(value="4.10", version=2, revision_of_observation_id=original.observation_id)
    store.ingest(original)
    store.ingest(revision)
    series = store.snapshot().series[0]
    assert len(series.observations) == 2
    assert len(series.current_observations) == 1
    assert series.current_observations[0].value.value == Decimal("4.10")
    history = store.vintage_history(original.series_key, original.reference_period)
    assert tuple(item.observation_version for item in history) == (1, 2)
    with pytest.raises(FrozenInstanceError):
        history[0].value = EconomicValue(value=Decimal("1"), unit=EconomicValueUnit.PERCENT)  # type: ignore[misc]


def test_store_collapses_duplicates_and_rejects_same_version_conflicts() -> None:
    store = MacroSeriesStore()
    original = _observation()
    store.ingest(original)
    store.ingest(_observation(received_at=MOMENT + timedelta(days=1)))
    assert store.observation_count == 1
    with pytest.raises(ValueError, match="conflicting macro-observation"):
        store.ingest(_observation(value="9.99"))


def test_store_detects_conflict_after_full_record_eviction_with_bounded_metadata() -> None:
    store = MacroSeriesStore(
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
    with pytest.raises(ValueError, match="conflicting macro-observation"):
        store.ingest(_observation(0, value="7.00"))
    assert store.version_identity_count <= 2


def test_post_eviction_duplicate_and_source_or_period_variants_remain_deterministic() -> None:
    store = MacroSeriesStore(
        max_series=3,
        max_occurrences_per_series=1,
        max_vintages_per_occurrence=1,
        max_version_identities=4,
    )
    original = _observation(0)
    store.ingest(original)
    store.ingest(_observation(1))
    duplicate = _observation(0, received_at=MOMENT + timedelta(days=8))
    store.ingest(duplicate)
    assert store.get_series(original.series_key) is not None
    other_period = _observation(2, value="9.99")
    store.ingest(other_period)
    other_source = MacroSeriesSource(
        source_id="another-macro-source",
        display_name="Another Macro Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.ECONOMIC_DATA,
    )
    store.ingest(
        _observation(
            0,
            source=other_source,
            key=_key(source_id=other_source.source_id),
            value="9.99",
        )
    )
    assert store.series_count == 2
    assert store.get_series(other_period.series_key) is not None


def test_store_bounds_vintages_occurrences_and_series_deterministically() -> None:
    store = MacroSeriesStore(
        max_series=2,
        max_occurrences_per_series=2,
        max_vintages_per_occurrence=2,
    )
    first = _observation(0)
    for version, value in ((1, "4.00"), (2, "4.01"), (3, "4.02")):
        revision_of = first.observation_id if version > 1 else None
        store.ingest(_observation(0, value=value, version=version, revision_of_observation_id=revision_of))
    for index in (1, 2):
        store.ingest(_observation(index))
    store.ingest(_observation(key=_key(source_series_id="B")))
    store.ingest(_observation(key=_key(source_series_id="C")))
    snapshot = store.snapshot()
    assert len(snapshot.series) == 2
    assert all(len(item.current_observations) <= 2 for item in snapshot.series)
    assert all(len(item.observations) <= 4 for item in snapshot.series)


def test_snapshot_is_insertion_order_independent_and_thread_safe() -> None:
    observations = tuple(_observation(index) for index in range(12))
    first = MacroSeriesStore()
    second = MacroSeriesStore()
    for observation in observations:
        first.ingest(observation)
    with ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(second.ingest, reversed(observations)))
    assert first.snapshot() == second.snapshot()


def test_evidence_routing_and_one_snapshot_run_are_macro_specific() -> None:
    store = EvidenceStore()
    store.ingest_macro_observation(_observation())
    snapshot = store.snapshot(symbol="XAUUSD")
    router = EvidenceRouter()
    assert len(router.route(agent_id="atlas_macro", snapshot=snapshot).macro_series) == 1
    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert ledger.macro_series == ()
    assert len(ledger.provenance) == 1
    for agent_id in (
        "capitol_smart_money",
        "scout_recon",
        "athena_analyst",
        "chartist_technician",
        "oracle_quant",
        "sentinel_risk_officer",
        "pilot_execution",
    ):
        assert router.route(agent_id=agent_id, snapshot=snapshot).macro_series == ()

    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    run = core._analyze_run(  # noqa: SLF001 - validates internal macro artifacts.
        AnalyzeRequest(symbol="XAUUSD", query="macro yield review"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    atlas = artifacts["atlas_macro"]
    assert len(atlas.macro_observations) == 1
    assert atlas.macro_observations[0].value.value == Decimal("4.25")
    assert atlas.stance is SpecialistStance.INSUFFICIENT_DATA
    assert not hasattr(atlas.macro_observations[0], "direction")
    assert artifacts["pilot_execution"].operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert artifacts["pilot_execution"].macro_observations == ()
    assert run.master_synthesis.specialist_analyses


def test_analyze_captures_one_macro_snapshot_for_every_specialist() -> None:
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
    store.ingest_macro_observation(_observation())
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
        evidence_router=router,
    )
    core.analyze(AnalyzeRequest(symbol="XAUUSD", query="macro yield review"))
    assert store.snapshot_calls == 1
    assert len(router.snapshot_ids) == 9
    assert len(set(router.snapshot_ids)) == 1


def test_macro_evidence_cannot_satisfy_market_requirements_or_change_public_api() -> None:
    store = EvidenceStore()
    store.ingest_macro_observation(_observation())
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    response = core.analyze(AnalyzeRequest(symbol="XAUUSD", query="macro yield review"))
    workflow = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="macro yield review")
    )
    assert set(response.model_dump()) == {
        "analysis_id", "mode", "symbol", "query", "selected_agents", "routing", "agent_results", "synthesis"
    }
    assert workflow.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    assert workflow.proposal.parameters.direction is None
    assert workflow.proposal.parameters.reference_price is None


def test_internal_ingestion_service_uses_the_provider_neutral_boundary() -> None:
    store = EvidenceStore()

    class Consumer:
        def ingest_macro_observation(self, observation: NormalizedMacroObservation) -> object:
            return store.ingest_macro_observation(observation)

    service = MacroSeriesIngestionService(consumer=Consumer())
    result = service.ingest(_observation())
    assert result == store.snapshot().macro_series[0]
    with pytest.raises(TypeError, match="NormalizedMacroObservation"):
        service.ingest(object())  # type: ignore[arg-type]
