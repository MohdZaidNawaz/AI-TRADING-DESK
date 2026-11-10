"""Canonical macro indicator, source, freshness, and availability policies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.economic_events import (
    EconomicReleaseStatus,
    EconomicValue,
    EconomicValueScale,
    EconomicValueUnit,
    NormalizedEconomicEvent,
)
from app.models.macro_intelligence import (
    InstrumentMacroClass,
    InstrumentMacroPolicy,
    MacroEvidenceAvailability,
    MacroFactor,
    MacroFreshnessBasis,
    MacroFreshnessPolicy,
    MacroFreshnessPolicyId,
    MacroFreshnessStatus,
    MacroIndicatorCategory,
    MacroIndicatorDefinition,
    MacroSourcePolicy,
    MacroSourceType,
    MacroValueRole,
)
from app.models.macro_series import (
    MacroFrequency,
    MacroReferencePeriodKind,
    NormalizedMacroObservation,
    SeasonalAdjustment,
)


MACRO_FRESHNESS_POLICIES: tuple[MacroFreshnessPolicy, ...] = (
    MacroFreshnessPolicy(
        policy_id=MacroFreshnessPolicyId.DAILY_MARKET_CONTEXT,
        basis=MacroFreshnessBasis.PUBLISHED_OR_OBSERVED_AT,
        fresh_for=timedelta(days=3),
        aging_for=timedelta(days=7),
        description="Daily yields and dollar context require source observation or publication time.",
    ),
    MacroFreshnessPolicy(
        policy_id=MacroFreshnessPolicyId.MONTHLY_OBSERVATION,
        basis=MacroFreshnessBasis.PUBLISHED_AT,
        fresh_for=timedelta(days=45),
        aging_for=timedelta(days=75),
        description="Monthly observations age from publication, never local retrieval.",
    ),
    MacroFreshnessPolicy(
        policy_id=MacroFreshnessPolicyId.QUARTERLY_OBSERVATION,
        basis=MacroFreshnessBasis.PUBLISHED_AT,
        fresh_for=timedelta(days=110),
        aging_for=timedelta(days=200),
        description="Quarterly observations age from publication and retain vintage lineage.",
    ),
    MacroFreshnessPolicy(
        policy_id=MacroFreshnessPolicyId.POLICY_RATE,
        basis=MacroFreshnessBasis.EFFECTIVE_AT,
        fresh_for=timedelta(days=45),
        aging_for=timedelta(days=120),
        description="Policy rates age from their effective time; retrieval alone cannot refresh them.",
    ),
    MacroFreshnessPolicy(
        policy_id=MacroFreshnessPolicyId.SCHEDULED_EVENT,
        basis=MacroFreshnessBasis.SCHEDULED_AND_RETRIEVED_AT,
        fresh_for=timedelta(hours=24),
        aging_for=timedelta(hours=72),
        description="A future schedule is assessed using both event time and retrieval age.",
    ),
    MacroFreshnessPolicy(
        policy_id=MacroFreshnessPolicyId.RELEASED_EVENT,
        basis=MacroFreshnessBasis.RELEASED_AT,
        fresh_for=timedelta(days=2),
        aging_for=timedelta(days=7),
        description="Released events age from release time, not retrieval time.",
    ),
)

_FRESHNESS_BY_ID = {policy.policy_id: policy for policy in MACRO_FRESHNESS_POLICIES}


MACRO_SOURCE_POLICIES: tuple[MacroSourcePolicy, ...] = (
    MacroSourcePolicy(
        source_type=MacroSourceType.COMMERCIAL_OBSERVATION,
        allowed_value_roles=(MacroValueRole.ACTUAL, MacroValueRole.OBSERVED),
        description="Commercial observations may establish their own reported values, not consensus.",
    ),
    MacroSourcePolicy(
        source_type=MacroSourceType.OFFICIAL_OBSERVATION,
        allowed_value_roles=(
            MacroValueRole.ACTUAL,
            MacroValueRole.OBSERVED,
            MacroValueRole.REVISED_PREVIOUS,
        ),
        description="Official observations may establish reported values and revisions, not consensus.",
    ),
    MacroSourcePolicy(
        source_type=MacroSourceType.CENTRAL_BANK,
        allowed_value_roles=(MacroValueRole.ACTUAL, MacroValueRole.EFFECTIVE),
        description="Central-bank evidence may establish announced and effective policy values.",
    ),
    MacroSourcePolicy(
        source_type=MacroSourceType.ECONOMIC_CALENDAR,
        allowed_value_roles=(
            MacroValueRole.ACTUAL,
            MacroValueRole.FORECAST,
            MacroValueRole.PREVIOUS,
            MacroValueRole.REVISED_PREVIOUS,
        ),
        description="Calendar evidence keeps actual, consensus, and previous values separate.",
    ),
    MacroSourcePolicy(
        source_type=MacroSourceType.COMMERCIAL_CONSENSUS,
        allowed_value_roles=(MacroValueRole.FORECAST, MacroValueRole.PREVIOUS),
        may_establish_consensus=True,
        description="A declared consensus source may establish forecasts but not official actuals.",
    ),
    MacroSourcePolicy(
        source_type=MacroSourceType.MARKET_PRICE_DERIVED,
        allowed_value_roles=(MacroValueRole.OBSERVED,),
        description="Market-derived context is observed evidence, not an economic release.",
    ),
)


INSTRUMENT_MACRO_POLICIES: tuple[InstrumentMacroPolicy, ...] = (
    InstrumentMacroPolicy(
        instrument_class=InstrumentMacroClass.GOLD,
        required_factors=(
            MacroFactor.INFLATION,
            MacroFactor.POLICY_RATES,
            MacroFactor.REAL_YIELDS,
            MacroFactor.NOMINAL_YIELDS,
            MacroFactor.US_DOLLAR,
            MacroFactor.RISK_CONTEXT,
        ),
        description="Gold assessment categories are conditional context, not directional signals.",
    ),
    InstrumentMacroPolicy(
        instrument_class=InstrumentMacroClass.SILVER,
        required_factors=(
            MacroFactor.INDUSTRIAL_GROWTH,
            MacroFactor.INFLATION,
            MacroFactor.POLICY_RATES,
            MacroFactor.NOMINAL_YIELDS,
            MacroFactor.US_DOLLAR,
            MacroFactor.RISK_CONTEXT,
        ),
        description="Silver combines precious-metal and industrial-growth context without causality claims.",
    ),
    InstrumentMacroPolicy(
        instrument_class=InstrumentMacroClass.FOREX,
        required_factors=(
            MacroFactor.BASE_CURRENCY_POLICY,
            MacroFactor.QUOTE_CURRENCY_POLICY,
            MacroFactor.RATE_DIFFERENTIAL,
            MacroFactor.CURRENCY_SPECIFIC_RELEASES,
        ),
        description="Forex assessment requires evidence for both currencies and their rate context.",
    ),
)


def _indicator(
    indicator_id: str,
    display_name: str,
    category: MacroIndicatorCategory,
    *,
    country: str | None,
    region: str | None = None,
    currency: str | None,
    maturity: str | None = None,
    frequency: MacroFrequency,
    unit: EconomicValueUnit,
    scale: EconomicValueScale = EconomicValueScale.ONE,
    seasonal: SeasonalAdjustment,
    period_kind: MacroReferencePeriodKind,
    source_type: MacroSourceType,
    freshness: MacroFreshnessPolicyId,
) -> MacroIndicatorDefinition:
    return MacroIndicatorDefinition(
        indicator_id=indicator_id,
        display_name=display_name,
        category=category,
        country=country,
        region=region,
        currency=currency,
        maturity=maturity,
        expected_frequency=frequency,
        value_unit=unit,
        value_scale=scale,
        seasonal_adjustment=seasonal,
        reference_period_kind=period_kind,
        expected_source_type=source_type,
        freshness_policy_id=freshness,
    )


CANONICAL_MACRO_INDICATORS: tuple[MacroIndicatorDefinition, ...] = (
    _indicator("us_cpi_all_items_index", "US CPI All Items Index", MacroIndicatorCategory.INFLATION, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.NOT_SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_core_cpi_index", "US Core CPI Index", MacroIndicatorCategory.INFLATION, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.NOT_SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_pce_price_index", "US PCE Price Index", MacroIndicatorCategory.INFLATION, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_core_pce_price_index", "US Core PCE Price Index", MacroIndicatorCategory.INFLATION, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_nonfarm_payrolls", "US Nonfarm Payrolls", MacroIndicatorCategory.EMPLOYMENT, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.COUNT, scale=EconomicValueScale.THOUSAND, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_unemployment_rate", "US Unemployment Rate", MacroIndicatorCategory.EMPLOYMENT, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_average_hourly_earnings", "US Average Hourly Earnings", MacroIndicatorCategory.EMPLOYMENT, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.CURRENCY, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_real_gdp_level", "US Real GDP Level", MacroIndicatorCategory.GROWTH, country="US", currency="USD", frequency=MacroFrequency.QUARTERLY, unit=EconomicValueUnit.CURRENCY, scale=EconomicValueScale.BILLION, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.QUARTER, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.QUARTERLY_OBSERVATION),
    _indicator("us_retail_sales_level", "US Retail Sales Level", MacroIndicatorCategory.GROWTH, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.CURRENCY, scale=EconomicValueScale.MILLION, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_manufacturing_pmi", "US Manufacturing PMI", MacroIndicatorCategory.GROWTH, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.COMMERCIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("us_services_pmi", "US Services PMI", MacroIndicatorCategory.GROWTH, country="US", currency="USD", frequency=MacroFrequency.MONTHLY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.SEASONALLY_ADJUSTED, period_kind=MacroReferencePeriodKind.MONTH, source_type=MacroSourceType.COMMERCIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.MONTHLY_OBSERVATION),
    _indicator("fed_policy_rate", "Federal Reserve Policy Rate", MacroIndicatorCategory.MONETARY_POLICY, country="US", currency="USD", frequency=MacroFrequency.POLICY_RATE, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE, source_type=MacroSourceType.CENTRAL_BANK, freshness=MacroFreshnessPolicyId.POLICY_RATE),
    _indicator("ecb_policy_rate", "ECB Policy Rate", MacroIndicatorCategory.MONETARY_POLICY, country=None, region="Euro Area", currency="EUR", frequency=MacroFrequency.POLICY_RATE, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE, source_type=MacroSourceType.CENTRAL_BANK, freshness=MacroFreshnessPolicyId.POLICY_RATE),
    _indicator("boe_policy_rate", "Bank of England Policy Rate", MacroIndicatorCategory.MONETARY_POLICY, country="GB", currency="GBP", frequency=MacroFrequency.POLICY_RATE, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE, source_type=MacroSourceType.CENTRAL_BANK, freshness=MacroFreshnessPolicyId.POLICY_RATE),
    _indicator("boj_policy_rate", "Bank of Japan Policy Rate", MacroIndicatorCategory.MONETARY_POLICY, country="JP", currency="JPY", frequency=MacroFrequency.POLICY_RATE, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE, source_type=MacroSourceType.CENTRAL_BANK, freshness=MacroFreshnessPolicyId.POLICY_RATE),
    *tuple(
        _indicator(f"us_nominal_treasury_yield_{maturity.lower()}", f"US Nominal Treasury Yield {maturity}", MacroIndicatorCategory.NOMINAL_YIELD, country="US", currency="USD", maturity=maturity, frequency=MacroFrequency.DAILY, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.DATE, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.DAILY_MARKET_CONTEXT)
        for maturity in ("1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y", "30Y")
    ),
    *tuple(
        _indicator(f"us_real_treasury_yield_{maturity.lower()}", f"US Real Treasury Yield {maturity}", MacroIndicatorCategory.REAL_YIELD, country="US", currency="USD", maturity=maturity, frequency=MacroFrequency.DAILY, unit=EconomicValueUnit.PERCENT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.DATE, source_type=MacroSourceType.OFFICIAL_OBSERVATION, freshness=MacroFreshnessPolicyId.DAILY_MARKET_CONTEXT)
        for maturity in ("5Y", "10Y", "30Y")
    ),
    _indicator("us_dollar_index_context", "US Dollar Index Context", MacroIndicatorCategory.US_DOLLAR, country="US", currency="USD", frequency=MacroFrequency.DAILY, unit=EconomicValueUnit.INDEX_POINT, seasonal=SeasonalAdjustment.NOT_APPLICABLE, period_kind=MacroReferencePeriodKind.DATE, source_type=MacroSourceType.MARKET_PRICE_DERIVED, freshness=MacroFreshnessPolicyId.DAILY_MARKET_CONTEXT),
)

_INDICATOR_BY_ID = {item.indicator_id: item for item in CANONICAL_MACRO_INDICATORS}
if len(_INDICATOR_BY_ID) != len(CANONICAL_MACRO_INDICATORS):  # pragma: no cover
    raise RuntimeError("canonical macro indicator IDs must be unique")


def get_macro_indicator(indicator_id: str) -> MacroIndicatorDefinition:
    try:
        return _INDICATOR_BY_ID[indicator_id]
    except KeyError as error:
        raise ValueError(f"unknown canonical macro indicator: {indicator_id}") from error


def get_freshness_policy(policy_id: MacroFreshnessPolicyId) -> MacroFreshnessPolicy:
    return _FRESHNESS_BY_ID[policy_id]


def economic_values_compatible(*values: EconomicValue | None) -> bool:
    present = tuple(value for value in values if value is not None)
    return bool(present) and len({(value.unit, value.scale) for value in present}) == 1


def validate_macro_observation(observation: NormalizedMacroObservation) -> None:
    definition = get_macro_indicator(observation.series_key.indicator_id)
    key = observation.series_key
    mismatches: list[str] = []
    comparisons = (
        (key.frequency, definition.expected_frequency, "frequency"),
        (key.value_unit, definition.value_unit, "unit"),
        (key.value_scale, definition.value_scale, "scale"),
        (key.seasonal_adjustment, definition.seasonal_adjustment, "seasonal_adjustment"),
        (observation.reference_period.kind, definition.reference_period_kind, "reference_period"),
        (key.country, definition.country, "country"),
        (key.region, definition.region, "region"),
        (key.currency, definition.currency, "currency"),
        (key.maturity, definition.maturity, "maturity"),
    )
    for actual, expected, name in comparisons:
        if actual != expected:
            mismatches.append(name)
    if mismatches:
        raise ValueError("macro observation is incompatible with canonical indicator: " + ",".join(mismatches))
    _validate_observation_chronology(observation)


def observation_available_as_of(
    observation: NormalizedMacroObservation,
    evaluated_at: datetime,
) -> bool:
    definition = get_macro_indicator(observation.series_key.indicator_id)
    validate_macro_observation(observation)
    availability = _observation_availability(observation, definition)
    return availability is not None and availability.available_as_of(evaluated_at)


def evaluate_macro_freshness(
    observation: NormalizedMacroObservation | None,
    *,
    evaluated_at: datetime,
) -> MacroFreshnessStatus:
    evaluated = _aware_utc(evaluated_at, "evaluation time")
    if observation is None:
        return MacroFreshnessStatus.NOT_AVAILABLE
    definition = get_macro_indicator(observation.series_key.indicator_id)
    validate_macro_observation(observation)
    policy = get_freshness_policy(definition.freshness_policy_id)
    availability = _observation_availability(observation, definition)
    if availability is None or not availability.available_as_of(evaluated):
        return MacroFreshnessStatus.UNKNOWN
    basis = _observation_freshness_basis(observation, policy.basis)
    if basis is None:
        return MacroFreshnessStatus.UNKNOWN
    return _age_status(evaluated - basis, policy)


def evaluate_event_freshness(
    event: NormalizedEconomicEvent | None,
    *,
    evaluated_at: datetime,
) -> MacroFreshnessStatus:
    evaluated = _aware_utc(evaluated_at, "evaluation time")
    if event is None:
        return MacroFreshnessStatus.NOT_AVAILABLE
    if event.release_status is EconomicReleaseStatus.SCHEDULED:
        policy = get_freshness_policy(MacroFreshnessPolicyId.SCHEDULED_EVENT)
        if event.scheduled_at < evaluated:
            return MacroFreshnessStatus.STALE
        return _age_status(evaluated - event.received_at, policy)
    if event.release_status in {EconomicReleaseStatus.RELEASED, EconomicReleaseStatus.REVISED}:
        policy = get_freshness_policy(MacroFreshnessPolicyId.RELEASED_EVENT)
        if event.released_at is None:
            return MacroFreshnessStatus.UNKNOWN
        if event.released_at > event.received_at:
            raise ValueError("economic release time cannot follow retrieval time")
        return _age_status(evaluated - event.released_at, policy)
    return MacroFreshnessStatus.UNKNOWN


def _observation_availability(
    observation: NormalizedMacroObservation,
    definition: MacroIndicatorDefinition,
) -> MacroEvidenceAvailability | None:
    bases: tuple[str, ...]
    policy = get_freshness_policy(definition.freshness_policy_id)
    if observation.observation_version > 1:
        if observation.vintage_at is None:
            return None
        bases = ("vintage_at",)
        if observation.published_at is not None:
            bases = ("published_at", "vintage_at")
    elif policy.basis is MacroFreshnessBasis.EFFECTIVE_AT:
        bases = ("effective_at",)
    elif policy.basis in {
        MacroFreshnessBasis.PUBLISHED_AT,
        MacroFreshnessBasis.PUBLISHED_OR_OBSERVED_AT,
    }:
        if observation.published_at is not None:
            bases = ("published_at",)
        elif policy.basis is MacroFreshnessBasis.PUBLISHED_OR_OBSERVED_AT:
            # Observed values require an explicit publication substitute.  It is
            # copied into the availability contract rather than treating local
            # receipt as evidence freshness.
            return MacroEvidenceAvailability(
                reference_period_start=observation.reference_period.start_date,
                reference_period_end=observation.reference_period.end_date,
                published_at=observation.observed_at,
                effective_at=observation.effective_at,
                retrieved_at=observation.received_at,
                vintage_at=observation.vintage_at,
                required_bases=("published_at",),
            )
        else:
            return None
    else:  # pragma: no cover - series policies cannot use event-only bases
        bases = ("published_at",)
    return MacroEvidenceAvailability(
        reference_period_start=observation.reference_period.start_date,
        reference_period_end=observation.reference_period.end_date,
        published_at=observation.published_at,
        effective_at=observation.effective_at,
        retrieved_at=observation.received_at,
        vintage_at=observation.vintage_at,
        required_bases=bases,  # type: ignore[arg-type]
    )


def _validate_observation_chronology(observation: NormalizedMacroObservation) -> None:
    if observation.published_at is not None and observation.published_at > observation.received_at:
        raise ValueError("macro publication time cannot follow retrieval time")
    if observation.vintage_at is not None and observation.vintage_at > observation.received_at:
        raise ValueError("macro vintage time cannot follow retrieval time")
    if (
        observation.published_at is not None
        and observation.vintage_at is not None
        and observation.vintage_at < observation.published_at
    ):
        raise ValueError("macro vintage time cannot precede publication time")


def _observation_freshness_basis(
    observation: NormalizedMacroObservation,
    basis: MacroFreshnessBasis,
) -> datetime | None:
    if observation.observation_version > 1:
        return observation.vintage_at
    if basis is MacroFreshnessBasis.PUBLISHED_AT:
        return observation.published_at
    if basis is MacroFreshnessBasis.PUBLISHED_OR_OBSERVED_AT:
        return observation.published_at or observation.observed_at
    if basis is MacroFreshnessBasis.EFFECTIVE_AT:
        return observation.effective_at
    return None


def _age_status(age: timedelta, policy: MacroFreshnessPolicy) -> MacroFreshnessStatus:
    if age < timedelta(0):
        return MacroFreshnessStatus.UNKNOWN
    if age <= policy.fresh_for:
        return MacroFreshnessStatus.FRESH
    if age <= policy.aging_for:
        return MacroFreshnessStatus.AGING
    return MacroFreshnessStatus.STALE


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)
