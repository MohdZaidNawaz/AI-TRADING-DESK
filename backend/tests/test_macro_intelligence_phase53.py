from __future__ import annotations

from calendar import monthrange
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence.models import EconomicEventEvidence, EvidenceSnapshot, MacroSeriesEvidence
from app.evidence.store import EvidenceStore
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
    EconomicValueProvenance,
    EconomicValueUnit,
    NormalizedEconomicEvent,
)
from app.models.events import DataDomain, SourceTransport
from app.models.macro_intelligence import (
    InstrumentMacroClass,
    MacroAssessmentStatus,
    MacroDirection,
    MacroFreshnessStatus,
)
from app.models.macro_series import (
    MacroReferencePeriod,
    MacroReferencePeriodKind,
    MacroSeriesKey,
    MacroSeriesSource,
    NormalizedMacroObservation,
)
from app.services.macro_intelligence import MacroIntelligenceService
from app.services.macro_policies import get_macro_indicator
from app.models.schemas import AnalyzeRequest


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _period(indicator_id: str, end: date) -> MacroReferencePeriod:
    kind = get_macro_indicator(indicator_id).reference_period_kind
    if kind is MacroReferencePeriodKind.MONTH:
        return MacroReferencePeriod(
            kind=kind,
            start_date=end.replace(day=1),
            end_date=end.replace(day=monthrange(end.year, end.month)[1]),
        )
    if kind is MacroReferencePeriodKind.QUARTER:
        quarter_start = 3 * ((end.month - 1) // 3) + 1
        quarter_end_month = quarter_start + 2
        return MacroReferencePeriod(
            kind=kind,
            start_date=date(end.year, quarter_start, 1),
            end_date=date(end.year, quarter_end_month, monthrange(end.year, quarter_end_month)[1]),
        )
    return MacroReferencePeriod(kind=kind, start_date=end, end_date=end)


def _observation(
    indicator_id: str,
    value: str,
    *,
    end: date,
    source_id: str = "fixture-official",
    available_at: datetime | None = None,
    received_at: datetime | None = None,
    version: int = 1,
    revision_of: str | None = None,
) -> NormalizedMacroObservation:
    definition = get_macro_indicator(indicator_id)
    available = available_at or NOW - timedelta(days=1)
    received = received_at or available
    source = MacroSeriesSource(
        source_id=source_id,
        display_name=f"{source_id} source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.ECONOMIC_DATA,
        origin_id=f"{source_id}-origin",
        origin_name=f"{source_id} publisher",
    )
    key = MacroSeriesKey(
        source_id=source_id,
        source_series_id=f"{source_id}-{indicator_id}",
        indicator_id=indicator_id,
        country=definition.country,
        region=definition.region,
        currency=definition.currency,
        maturity=definition.maturity,
        frequency=definition.expected_frequency,
        seasonal_adjustment=definition.seasonal_adjustment,
        value_unit=definition.value_unit,
        value_scale=definition.value_scale,
    )
    is_policy = definition.reference_period_kind is MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE
    return NormalizedMacroObservation(
        source=source,
        series_key=key,
        reference_period=_period(indicator_id, end),
        value=EconomicValue(
            value=Decimal(value),
            unit=definition.value_unit,
            scale=definition.value_scale,
        ),
        observation_version=version,
        revision_of_observation_id=revision_of,
        observed_at=available,
        effective_at=available if is_policy else None,
        published_at=None if is_policy else available,
        vintage_at=available if version > 1 else None,
        received_at=received,
    )


def _series(*observations: NormalizedMacroObservation) -> MacroSeriesEvidence:
    return MacroSeriesEvidence.from_observations(
        observations[0].series_key,
        tuple(observations),
    )


def _event(
    *,
    actual: str,
    forecast: str | None,
    previous: str | None = None,
    revised_previous: str | None = None,
    released_at: datetime = NOW - timedelta(hours=2),
) -> EconomicEventEvidence:
    definition = get_macro_indicator("us_cpi_all_items_index")
    value = lambda raw: (
        EconomicValue(value=Decimal(raw), unit=definition.value_unit, scale=definition.value_scale)
        if raw is not None
        else None
    )
    source = EconomicEventSource(
        source_id="fixture-calendar",
        display_name="Fixture calendar",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.ECONOMIC_DATA,
        origin_id="bls",
        origin_name="US Bureau of Labor Statistics",
    )
    forecast_provenance = (
        EconomicValueProvenance(
            retrieval_provider_id="fixture-calendar",
            retrieval_provider_name="Fixture calendar",
            role="consensus",
            retrieved_at=released_at,
        )
        if forecast is not None
        else None
    )
    event = NormalizedEconomicEvent(
        source=source,
        occurrence_id=f"cpi-{released_at.isoformat()}-{actual}-{forecast}",
        event_name="US CPI release",
        indicator_id="us_cpi_all_items_index",
        country="US",
        currency="USD",
        reference_period="2026-10",
        scheduled_at=released_at,
        released_at=released_at,
        release_status=EconomicReleaseStatus.RELEASED,
        expected_impact=EconomicImpact.HIGH,
        previous=value(previous),
        revised_previous=value(revised_previous),
        forecast=value(forecast),
        forecast_provenance=forecast_provenance,
        actual=value(actual),
        release_version=1,
        received_at=released_at,
    )
    return EconomicEventEvidence.from_event(event)


def _report(
    subject: str,
    *series: MacroSeriesEvidence,
    events: tuple[EconomicEventEvidence, ...] = (),
):
    return MacroIntelligenceService().analyze(
        subject=subject,
        snapshot=EvidenceSnapshot(macro_series=tuple(series), economic_events=events),
        evaluated_at=NOW,
    )


def test_release_surprise_and_revision_use_typed_evidence_without_zero_fallback() -> None:
    report = _report(
        "XAU/USD",
        events=(
            _event(actual="311.2", forecast="310.7", previous="309.8", revised_previous="310.0"),
            _event(actual="311.2", forecast=None),
            _event(actual="1.0", forecast="0"),
        ),
    )
    available = next(item for item in report.release_surprises if item.status is MacroAssessmentStatus.AVAILABLE)
    missing = next(item for item in report.release_surprises if item.status is MacroAssessmentStatus.INSUFFICIENT_DATA)
    revision = report.release_revisions[0]
    assert available.surprise is not None and available.surprise.value == Decimal("0.5")
    assert available.direction is MacroDirection.ABOVE
    assert "forecast" in missing.missing_inputs and missing.surprise is None
    zero_forecast = next(
        item for item in report.release_surprises if item.forecast is not None and item.forecast.value == 0
    )
    assert zero_forecast.status is MacroAssessmentStatus.AVAILABLE
    assert zero_forecast.surprise is not None and zero_forecast.surprise.value == Decimal("1.0")
    assert revision.revision is not None and revision.revision.value == Decimal("0.2")
    assert revision.direction is MacroDirection.RISING


def test_incompatible_release_values_and_series_definition_mismatches_fail_closed() -> None:
    event = _event(actual="311.2", forecast="310.7")
    incompatible_event = replace(
        event,
        forecast=EconomicValue(value=Decimal("3.0"), unit=EconomicValueUnit.PERCENT),
    )
    valid_series = _series(
        _observation("us_nominal_treasury_yield_10y", "4.4", end=date(2026, 11, 4))
    )
    wrong_maturity = replace(
        valid_series,
        key=valid_series.key.model_copy(update={"maturity": "2Y"}),
    )
    report = _report("XAU/USD", wrong_maturity, events=(incompatible_event,))
    assert report.release_surprises[0].status is MacroAssessmentStatus.INCOMPATIBLE
    assert report.release_surprises[0].surprise is None
    assert report.observations == ()


def test_release_identity_mismatch_is_invalid_and_future_release_is_excluded() -> None:
    event = _event(actual="311.2", forecast="310.7")
    wrong_currency = replace(event, currency="EUR")
    future = _event(
        actual="311.2",
        forecast="310.7",
        released_at=NOW + timedelta(hours=1),
    )
    report = _report("XAU/USD", events=(wrong_currency, future))
    assert len(report.release_surprises) == 1
    assert report.release_surprises[0].status is MacroAssessmentStatus.INVALID_INPUT
    assert any("currency_mismatch" in item for item in report.release_surprises[0].warnings)
    assert report.release_surprises[0].surprise is None


def test_trend_uses_chronological_periods_and_reports_insufficient_history() -> None:
    september = _observation("us_cpi_all_items_index", "308.0", end=date(2026, 9, 30))
    october = _observation("us_cpi_all_items_index", "310.0", end=date(2026, 10, 31))
    single = _observation("us_unemployment_rate", "4.1", end=date(2026, 10, 31))
    report = _report("XAU/USD", _series(september, october), _series(single))
    cpi = next(item for item in report.indicator_changes if item.indicator_id == "us_cpi_all_items_index")
    unemployment = next(item for item in report.indicator_changes if item.indicator_id == "us_unemployment_rate")
    assert cpi.status is MacroAssessmentStatus.AVAILABLE
    assert cpi.change is not None and cpi.change.value == Decimal("2.0")
    assert cpi.direction is MacroDirection.RISING
    assert cpi.previous_period_end < cpi.current_period_end
    assert unemployment.status is MacroAssessmentStatus.INSUFFICIENT_DATA


def test_as_of_selection_keeps_available_version_when_revision_is_future() -> None:
    first = _observation("us_cpi_all_items_index", "310.0", end=date(2026, 10, 31))
    future_revision = _observation(
        "us_cpi_all_items_index",
        "309.5",
        end=date(2026, 10, 31),
        available_at=NOW + timedelta(days=1),
        received_at=NOW + timedelta(days=1),
        version=2,
        revision_of=first.observation_id,
    )
    report = _report("XAU/USD", _series(first, future_revision))
    assert len(report.observations) == 1
    assert report.observations[0].observation_id == first.observation_id
    assert report.observations[0].value.value == Decimal("310.0")


def test_policy_differential_and_both_yield_spreads_are_calculated_from_fresh_data() -> None:
    series = (
        _series(_observation("fed_policy_rate", "5.25", end=date(2026, 10, 15))),
        _series(_observation("ecb_policy_rate", "4.00", end=date(2026, 10, 15))),
        _series(_observation("us_nominal_treasury_yield_3m", "5.10", end=date(2026, 11, 4))),
        _series(_observation("us_nominal_treasury_yield_2y", "4.80", end=date(2026, 11, 4))),
        _series(_observation("us_nominal_treasury_yield_10y", "4.40", end=date(2026, 11, 4))),
    )
    report = _report("EUR/USD", *series)
    differential = next(
        item for item in report.policy_rate_differentials if {item.base_currency, item.quote_currency} == {"USD", "EUR"}
    )
    assert differential.status is MacroAssessmentStatus.AVAILABLE
    assert differential.differential is not None and differential.differential.value == Decimal("1.25")
    assert {(item.short_maturity, item.long_maturity) for item in report.yield_curve_spreads} == {
        ("3M", "10Y"),
        ("2Y", "10Y"),
    }
    assert all(item.status is MacroAssessmentStatus.AVAILABLE for item in report.yield_curve_spreads)


def test_mismatched_yield_dates_fail_closed_and_real_yield_is_never_synthesized() -> None:
    report = _report(
        "XAU/USD",
        _series(_observation("us_nominal_treasury_yield_2y", "4.8", end=date(2026, 11, 3))),
        _series(_observation("us_nominal_treasury_yield_10y", "4.4", end=date(2026, 11, 4))),
    )
    assert report.yield_curve_spreads[0].status is MacroAssessmentStatus.INCOMPATIBLE
    assert report.yield_curve_spreads[0].spread is None
    assert report.real_yield_contexts == ()


def test_direct_real_yield_and_conflicting_sources_preserve_provenance() -> None:
    first = _series(
        _observation("us_real_treasury_yield_10y", "2.10", end=date(2026, 11, 4), source_id="us_treasury")
    )
    conflicting = _series(
        _observation("us_real_treasury_yield_10y", "2.20", end=date(2026, 11, 4), source_id="fred")
    )
    report = _report("XAU/USD", first, conflicting)
    context = report.real_yield_contexts[0]
    assessment = report.instrument_assessments[0]
    assert context.status is MacroAssessmentStatus.AVAILABLE
    assert context.real_yield is not None and context.nominal_yield is None
    assert len(assessment.conflicting_evidence_ids) == 2
    assert {item.retrieval_provider_id for item in report.observations} == {"us_treasury", "fred"}
    assert all(item.retrieved_at is not None for item in report.observations)


def test_instrument_assessments_remain_conditional_and_non_authorizing() -> None:
    common = (
        _series(_observation("fed_policy_rate", "5.25", end=date(2026, 10, 15))),
        _series(_observation("ecb_policy_rate", "4.00", end=date(2026, 10, 15))),
    )
    for subject, expected_class in (
        ("XAU/USD", InstrumentMacroClass.GOLD),
        ("XAG/USD", InstrumentMacroClass.SILVER),
        ("EUR/USD", InstrumentMacroClass.FOREX),
    ):
        report = _report(subject, *common)
        assessment = report.instrument_assessments[0]
        assert assessment.instrument_class is expected_class
        assert assessment.status is MacroAssessmentStatus.INSUFFICIENT_DATA
        assert assessment.missing_inputs
        assert report.production_authorization_available is False
        assert report.submission_allowed is False
        assert report.execution_authority is False


def test_stale_and_unknown_inputs_are_not_reported_available() -> None:
    stale = _observation(
        "fed_policy_rate",
        "5.25",
        end=date(2026, 1, 1),
        available_at=NOW - timedelta(days=200),
        received_at=NOW - timedelta(days=200),
    )
    fresh = _observation("ecb_policy_rate", "4.00", end=date(2026, 10, 15))
    report = _report("EUR/USD", _series(stale), _series(fresh))
    differential = report.policy_rate_differentials[0]
    assert differential.status is MacroAssessmentStatus.STALE
    assert MacroFreshnessStatus.STALE in differential.evidence_freshness
    assert differential.differential is None


def test_atlas_and_master_receive_deterministic_macro_calculations() -> None:
    store = EvidenceStore()
    store.ingest_macro_observation(
        _observation("us_cpi_all_items_index", "308.0", end=date(2026, 9, 30))
    )
    store.ingest_macro_observation(
        _observation("us_cpi_all_items_index", "310.0", end=date(2026, 10, 31))
    )
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    response = core.analyze(AnalyzeRequest(symbol="XAUUSD", query="macro inflation review"))
    atlas = next(item for item in response.agent_results if item.agent_id == "atlas_macro")
    assert response.macro_intelligence is not None
    assert any(item.status is MacroAssessmentStatus.AVAILABLE for item in response.macro_intelligence.indicator_changes)
    assert "period change" in atlas.summary
    assert "period change" in response.synthesis.final_response
    assert response.macro_intelligence.production_authorization_available is False
