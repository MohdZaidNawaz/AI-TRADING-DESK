from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.evidence.economic_store import EconomicEventStore
from app.evidence.macro_store import MacroSeriesStore
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
    EconomicValueScale,
    EconomicValueUnit,
    NormalizedEconomicEvent,
)
from app.models.events import DataDomain, SourceTransport
from app.models.macro_intelligence import (
    EconomicReleaseSurprise,
    InstrumentMacroClass,
    MacroAssessmentStatus,
    MacroEvidenceAvailability,
    MacroFreshnessPolicy,
    MacroFreshnessPolicyId,
    MacroFreshnessStatus,
    MacroIntelligenceReport,
    MacroProviderDiagnostic,
    MacroProviderStatus,
    MacroSourceType,
)
from app.models.macro_series import (
    MacroFrequency,
    MacroReferencePeriod,
    MacroReferencePeriodKind,
    MacroSeriesKey,
    MacroSeriesSource,
    NormalizedMacroObservation,
    SeasonalAdjustment,
)
from app.services.macro_policies import (
    CANONICAL_MACRO_INDICATORS,
    INSTRUMENT_MACRO_POLICIES,
    MACRO_FRESHNESS_POLICIES,
    MACRO_SOURCE_POLICIES,
    economic_values_compatible,
    evaluate_event_freshness,
    evaluate_macro_freshness,
    get_macro_indicator,
    observation_available_as_of,
    validate_macro_observation,
)


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _observation(
    *,
    indicator_id: str = "us_cpi_all_items_index",
    published_at: datetime | None = NOW - timedelta(days=20),
    received_at: datetime = NOW - timedelta(days=19),
    version: int = 1,
    vintage_at: datetime | None = None,
) -> NormalizedMacroObservation:
    definition = get_macro_indicator(indicator_id)
    if definition.reference_period_kind is MacroReferencePeriodKind.MONTH:
        period = MacroReferencePeriod(
            kind=MacroReferencePeriodKind.MONTH,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
    elif definition.reference_period_kind is MacroReferencePeriodKind.QUARTER:
        period = MacroReferencePeriod(
            kind=MacroReferencePeriodKind.QUARTER,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 9, 30),
        )
    else:
        period_date = (published_at or NOW).date()
        period = MacroReferencePeriod(
            kind=definition.reference_period_kind,
            start_date=period_date,
            end_date=period_date,
        )
    source = MacroSeriesSource(
        source_id="fixture-official",
        display_name="Fixture Official Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.ECONOMIC_DATA,
    )
    return NormalizedMacroObservation(
        source=source,
        series_key=MacroSeriesKey(
            source_id=source.source_id,
            source_series_id=f"fixture-{indicator_id}",
            indicator_id=definition.indicator_id,
            country=definition.country,
            region=definition.region,
            currency=definition.currency,
            maturity=definition.maturity,
            frequency=definition.expected_frequency,
            seasonal_adjustment=definition.seasonal_adjustment,
            value_unit=definition.value_unit,
            value_scale=definition.value_scale,
        ),
        reference_period=period,
        value=EconomicValue(
            value=Decimal("123.4"),
            unit=definition.value_unit,
            scale=definition.value_scale,
        ),
        observation_version=version,
        revision_of_observation_id=("prior-observation" if version > 1 else None),
        observed_at=published_at,
        effective_at=(published_at if definition.expected_frequency is MacroFrequency.POLICY_RATE else None),
        published_at=published_at,
        vintage_at=vintage_at,
        received_at=received_at,
    )


def _event(
    *,
    status: EconomicReleaseStatus,
    scheduled_at: datetime,
    released_at: datetime | None = None,
    received_at: datetime = NOW,
) -> NormalizedEconomicEvent:
    return NormalizedEconomicEvent(
        source=EconomicEventSource(
            source_id="fixture-calendar",
            display_name="Fixture Calendar",
                transport=SourceTransport.MANUAL,
            domain=DataDomain.ECONOMIC_DATA,
        ),
        occurrence_id="fixture-event-1",
        event_name="Fixture release",
        indicator_id="fixture_release",
        country="US",
        currency="USD",
        scheduled_at=scheduled_at,
        released_at=released_at,
        release_status=status,
        expected_impact=EconomicImpact.HIGH,
        actual=(
            EconomicValue(value=Decimal("1"), unit=EconomicValueUnit.PERCENT)
            if status in {EconomicReleaseStatus.RELEASED, EconomicReleaseStatus.REVISED}
            else None
        ),
        release_version=1,
        received_at=received_at,
    )


def test_canonical_indicator_catalogue_is_unique_complete_and_provider_neutral() -> None:
    identities = {item.indicator_id for item in CANONICAL_MACRO_INDICATORS}
    assert len(identities) == len(CANONICAL_MACRO_INDICATORS)
    assert {
        "us_cpi_all_items_index",
        "us_core_cpi_index",
        "us_pce_price_index",
        "us_core_pce_price_index",
        "us_nonfarm_payrolls",
        "us_unemployment_rate",
        "us_average_hourly_earnings",
        "us_real_gdp_level",
        "us_retail_sales_level",
        "us_manufacturing_pmi",
        "us_services_pmi",
        "fed_policy_rate",
        "ecb_policy_rate",
        "boe_policy_rate",
        "boj_policy_rate",
        "us_nominal_treasury_yield_10y",
        "us_real_treasury_yield_10y",
        "us_dollar_index_context",
    } <= identities
    assert all(not item.indicator_id.startswith(("fred_", "bls_", "twelve_")) for item in CANONICAL_MACRO_INDICATORS)


def test_source_and_instrument_policies_are_complete_and_non_authorizing() -> None:
    assert {policy.source_type for policy in MACRO_SOURCE_POLICIES} == set(MacroSourceType)
    policies = {policy.instrument_class: policy for policy in INSTRUMENT_MACRO_POLICIES}
    assert set(policies) == set(InstrumentMacroClass)
    assert len(policies[InstrumentMacroClass.GOLD].required_factors) == 6
    assert len(policies[InstrumentMacroClass.SILVER].required_factors) == 6
    assert len(policies[InstrumentMacroClass.FOREX].required_factors) == 4


def test_observation_validation_rejects_unit_scale_frequency_and_period_mismatch() -> None:
    valid = _observation()
    validate_macro_observation(valid)
    for update in (
        {"value_unit": EconomicValueUnit.PERCENT},
        {"value_scale": EconomicValueScale.MILLION},
        {"frequency": MacroFrequency.QUARTERLY},
    ):
        key = valid.series_key.model_copy(update=update)
        incompatible = valid.model_copy(
            update={
                "series_key": key,
                "value": EconomicValue(value=valid.value.value, unit=key.value_unit, scale=key.value_scale),
            }
        )
        with pytest.raises(ValueError, match="incompatible"):
            validate_macro_observation(incompatible)

    wrong_period = valid.model_copy(
        update={
            "reference_period": MacroReferencePeriod(
                kind=MacroReferencePeriodKind.DATE,
                start_date=date(2026, 9, 30),
                end_date=date(2026, 9, 30),
            )
        }
    )
    with pytest.raises(ValueError, match="reference_period"):
        validate_macro_observation(wrong_period)


@pytest.mark.parametrize(
    ("age", "expected"),
    (
        (timedelta(days=45), MacroFreshnessStatus.FRESH),
        (timedelta(days=45, microseconds=1), MacroFreshnessStatus.AGING),
        (timedelta(days=75), MacroFreshnessStatus.AGING),
        (timedelta(days=75, microseconds=1), MacroFreshnessStatus.STALE),
    ),
)
def test_monthly_freshness_boundaries(age: timedelta, expected: MacroFreshnessStatus) -> None:
    observation = _observation(published_at=NOW - age, received_at=NOW)
    assert evaluate_macro_freshness(observation, evaluated_at=NOW) is expected


def test_missing_publication_is_unknown_and_recent_retrieval_cannot_refresh_old_data() -> None:
    missing = _observation(published_at=None, received_at=NOW)
    validate_macro_observation(missing)
    assert evaluate_macro_freshness(missing, evaluated_at=NOW) is MacroFreshnessStatus.UNKNOWN
    assert not observation_available_as_of(missing, NOW)
    old = _observation(published_at=NOW - timedelta(days=90), received_at=NOW)
    assert evaluate_macro_freshness(old, evaluated_at=NOW) is MacroFreshnessStatus.STALE
    assert evaluate_macro_freshness(None, evaluated_at=NOW) is MacroFreshnessStatus.NOT_AVAILABLE


def test_policy_rate_uses_effective_time_not_recent_retrieval() -> None:
    observation = _observation(
        indicator_id="fed_policy_rate",
        published_at=NOW - timedelta(days=130),
        received_at=NOW,
    )
    assert evaluate_macro_freshness(observation, evaluated_at=NOW) is MacroFreshnessStatus.STALE


def test_scheduled_and_released_event_freshness_are_distinct() -> None:
    scheduled = _event(
        status=EconomicReleaseStatus.SCHEDULED,
        scheduled_at=NOW + timedelta(days=2),
        received_at=NOW - timedelta(hours=24),
    )
    assert evaluate_event_freshness(scheduled, evaluated_at=NOW) is MacroFreshnessStatus.FRESH
    old_schedule = scheduled.model_copy(update={"received_at": NOW - timedelta(hours=73)})
    assert evaluate_event_freshness(old_schedule, evaluated_at=NOW) is MacroFreshnessStatus.STALE
    released = _event(
        status=EconomicReleaseStatus.RELEASED,
        scheduled_at=NOW - timedelta(days=3),
        released_at=NOW - timedelta(days=2),
        received_at=NOW,
    )
    assert evaluate_event_freshness(released, evaluated_at=NOW) is MacroFreshnessStatus.FRESH


def test_availability_contract_rejects_invalid_publication_and_vintage_chronology() -> None:
    with pytest.raises(ValidationError, match="publication time cannot follow retrieval"):
        MacroEvidenceAvailability(
            reference_period_start=date(2026, 9, 1),
            reference_period_end=date(2026, 9, 30),
            published_at=NOW + timedelta(days=1),
            retrieved_at=NOW,
            required_bases=("published_at",),
        )
    with pytest.raises(ValidationError, match="vintage time cannot precede publication"):
        MacroEvidenceAvailability(
            reference_period_start=date(2026, 9, 1),
            reference_period_end=date(2026, 9, 30),
            published_at=NOW - timedelta(days=1),
            vintage_at=NOW - timedelta(days=2),
            retrieved_at=NOW,
            required_bases=("published_at", "vintage_at"),
        )


def test_revision_cannot_be_used_before_vintage_availability() -> None:
    revision = _observation(
        published_at=NOW - timedelta(days=20),
        received_at=NOW,
        version=2,
        vintage_at=NOW - timedelta(days=1),
    )
    assert not observation_available_as_of(revision, NOW - timedelta(days=2))
    assert observation_available_as_of(revision, NOW - timedelta(days=1))


def test_economic_release_values_must_have_compatible_units_and_scales() -> None:
    percent = EconomicValue(value=Decimal("3.1"), unit=EconomicValueUnit.PERCENT)
    other_percent = EconomicValue(value=Decimal("3.0"), unit=EconomicValueUnit.PERCENT)
    count = EconomicValue(value=Decimal("200"), unit=EconomicValueUnit.COUNT, scale=EconomicValueScale.THOUSAND)
    assert economic_values_compatible(percent, other_percent)
    assert not economic_values_compatible(percent, count)
    assert not economic_values_compatible(None, None)


def test_release_assessment_rejects_incompatible_values_and_unavailable_result() -> None:
    percent = EconomicValue(value=Decimal("3.1"), unit=EconomicValueUnit.PERCENT)
    count = EconomicValue(
        value=Decimal("200"),
        unit=EconomicValueUnit.COUNT,
        scale=EconomicValueScale.THOUSAND,
    )
    with pytest.raises(ValidationError, match="compatible units"):
        EconomicReleaseSurprise(
            status=MacroAssessmentStatus.INSUFFICIENT_DATA,
            method_id="release_surprise",
            method_version="1.0",
            calculated_at=NOW,
            missing_inputs=("compatible_forecast",),
            actual=percent,
            forecast=count,
            confidence=0,
        )
    with pytest.raises(ValidationError, match="cannot contain a derived result"):
        EconomicReleaseSurprise(
            status=MacroAssessmentStatus.INSUFFICIENT_DATA,
            method_id="release_surprise",
            method_version="1.0",
            calculated_at=NOW,
            missing_inputs=("forecast",),
            surprise=percent,
            confidence=0,
        )


def test_assessment_contract_uses_explicit_insufficient_data_instead_of_fallback() -> None:
    assessment = EconomicReleaseSurprise(
        status=MacroAssessmentStatus.INSUFFICIENT_DATA,
        method_id="release_surprise",
        method_version="1.0",
        calculated_at=NOW,
        missing_inputs=("actual", "forecast"),
        limitations=("No calculation is performed in Phase 5.1.",),
        confidence=0,
    )
    assert assessment.surprise is None
    with pytest.raises(ValidationError, match="missing inputs"):
        EconomicReleaseSurprise(
            status=MacroAssessmentStatus.AVAILABLE,
            method_id="release_surprise",
            method_version="1.0",
            supporting_evidence_ids=("event-1",),
            calculated_at=NOW,
            missing_inputs=("forecast",),
            confidence=0.5,
        )


def test_available_assessment_requires_compatible_fresh_evidence() -> None:
    actual = EconomicValue(value=Decimal("3.1"), unit=EconomicValueUnit.PERCENT)
    forecast = EconomicValue(value=Decimal("3.0"), unit=EconomicValueUnit.PERCENT)
    surprise = EconomicValue(value=Decimal("0.1"), unit=EconomicValueUnit.PERCENT)
    assessment = EconomicReleaseSurprise(
        status=MacroAssessmentStatus.AVAILABLE,
        method_id="release_surprise",
        method_version="1.0",
        supporting_evidence_ids=("event-1",),
        evidence_freshness=(MacroFreshnessStatus.FRESH,),
        calculated_at=NOW,
        actual=actual,
        forecast=forecast,
        surprise=surprise,
        confidence=0.8,
    )
    assert assessment.surprise == surprise
    with pytest.raises(ValidationError, match="usable evidence freshness"):
        EconomicReleaseSurprise(
            status=MacroAssessmentStatus.AVAILABLE,
            method_id="release_surprise",
            method_version="1.0",
            supporting_evidence_ids=("event-1",),
            evidence_freshness=(MacroFreshnessStatus.STALE,),
            calculated_at=NOW,
            actual=actual,
            forecast=forecast,
            surprise=surprise,
            confidence=0.8,
        )


def test_report_is_non_authorizing_and_unconfigured_provider_is_truthful() -> None:
    report = MacroIntelligenceReport(
        subject="XAU/USD",
        evaluated_at=NOW,
        provider_diagnostics=(
            MacroProviderDiagnostic(
                provider_id=None,
                source_type=MacroSourceType.OFFICIAL_OBSERVATION,
                status=MacroProviderStatus.NOT_CONFIGURED,
                reason_codes=("NO_MACRO_PROVIDER_CONFIGURED",),
            ),
        ),
        missing_information=("official macro observations",),
    )
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
    with pytest.raises(ValidationError):
        MacroIntelligenceReport(
            subject="XAU/USD",
            evaluated_at=NOW,
            production_authorization_available=True,
        )


def test_existing_event_and_macro_stores_remain_compatible() -> None:
    event_store = EconomicEventStore()
    event = _event(
        status=EconomicReleaseStatus.SCHEDULED,
        scheduled_at=NOW + timedelta(days=1),
    )
    assert event_store.ingest(event).event_id == event.event_id

    macro_store = MacroSeriesStore()
    observation = _observation()
    evidence = macro_store.ingest(observation)
    assert evidence is not None
    assert evidence.observations[0].observation_id == observation.observation_id


def test_freshness_policy_windows_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="greater than fresh_for"):
        MacroFreshnessPolicy(
            policy_id=MacroFreshnessPolicyId.MONTHLY_OBSERVATION,
            basis=MACRO_FRESHNESS_POLICIES[1].basis,
            fresh_for=timedelta(days=45),
            aging_for=timedelta(days=45),
            description="invalid equal boundary",
        )
