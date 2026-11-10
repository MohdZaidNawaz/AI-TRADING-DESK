"""Deterministic Phase 5.3 macro intelligence from cached evidence only."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from app.evidence.models import (
    EconomicEventEvidence,
    EvidenceSnapshot,
    MacroObservationEvidence,
    MacroSeriesEvidence,
)
from app.models.economic_events import EconomicReleaseStatus, EconomicValue
from app.models.macro_intelligence import (
    CrossMarketObservation,
    EconomicReleaseRevision,
    EconomicReleaseSurprise,
    InstrumentMacroClass,
    InstrumentSpecificMacroAssessment,
    MacroAssessmentStatus,
    MacroDirection,
    MacroFactor,
    MacroFreshnessStatus,
    MacroIndicatorChange,
    MacroIntelligenceReport,
    MacroObservationSummary,
    MacroProviderDiagnostic,
    MacroProviderStatus,
    MacroSourceType,
    PolicyRateDifferential,
    RealYieldContext,
    YieldCurveSpread,
)
from app.models.macro_providers import MacroProviderId
from app.services.macro_policies import (
    CANONICAL_MACRO_INDICATORS,
    INSTRUMENT_MACRO_POLICIES,
    economic_values_compatible,
    get_freshness_policy,
    get_macro_indicator,
)


_CANONICAL_IDS = frozenset(item.indicator_id for item in CANONICAL_MACRO_INDICATORS)
_POLICY_RATE_BY_CURRENCY = {
    "USD": "fed_policy_rate",
    "EUR": "ecb_policy_rate",
    "GBP": "boe_policy_rate",
    "JPY": "boj_policy_rate",
}
_INFLATION_IDS = frozenset(
    {
        "us_cpi_all_items_index",
        "us_core_cpi_index",
        "us_pce_price_index",
        "us_core_pce_price_index",
    }
)
_GROWTH_IDS = frozenset(
    {
        "us_real_gdp_level",
        "us_retail_sales_level",
        "us_manufacturing_pmi",
        "us_services_pmi",
    }
)
_SOURCE_PRIORITY = {"us_treasury": 3, "bls": 3, "federal_reserve": 3, "fred": 2}


class MacroIntelligenceService:
    """Calculate explainable macro assessments without provider retrieval or signals."""

    def analyze(
        self,
        *,
        subject: str,
        snapshot: EvidenceSnapshot,
        provider_diagnostics: tuple[MacroProviderDiagnostic, ...] = (),
        evaluated_at: datetime | None = None,
    ) -> MacroIntelligenceReport:
        evaluated = _evaluation_time(evaluated_at)
        _, histories = _select_series_as_of(snapshot, evaluated)
        latest = {indicator: values[-1] for indicator, values in histories.items() if values}
        changes = tuple(
            _indicator_change(indicator, histories[indicator], evaluated)
            for indicator in sorted(histories)
        )
        surprises = tuple(
            result
            for event in snapshot.economic_events
            if (result := _surprise(event, evaluated)) is not None
        )
        revisions = tuple(
            result
            for event in snapshot.economic_events
            if (result := _revision(event, evaluated)) is not None
        )
        differentials = _policy_rate_differentials(latest, evaluated)
        spreads = _yield_spreads(latest, evaluated)
        real_yields = _real_yields(latest, evaluated)
        conflicts = _conflicting_evidence(snapshot, evaluated)
        summaries = _observation_summaries(snapshot, evaluated)
        cross_market = _cross_market_context(subject, latest, evaluated)
        assessment = _instrument_assessment(
            subject=subject,
            latest=latest,
            changes=changes,
            surprises=surprises,
            differentials=differentials,
            spreads=spreads,
            real_yields=real_yields,
            conflicts=conflicts,
            evaluated_at=evaluated,
        )
        present = set(latest)
        missing = {
            item.indicator_id
            for item in CANONICAL_MACRO_INDICATORS
            if item.indicator_id not in present
        }
        missing.update(assessment.missing_inputs)
        return MacroIntelligenceReport(
            subject=subject,
            evaluated_at=evaluated,
            provider_diagnostics=provider_diagnostics or default_provider_diagnostics(),
            observations=summaries,
            release_surprises=surprises,
            release_revisions=revisions,
            indicator_changes=changes,
            policy_rate_differentials=differentials,
            yield_curve_spreads=spreads,
            real_yield_contexts=real_yields,
            cross_market_observations=cross_market,
            instrument_assessments=(assessment,),
            missing_information=tuple(sorted(missing)),
            warnings=(
                "Macro observations are evidence inputs, not trading authorization.",
                "Conditional implications do not establish causation or price direction.",
            ),
        )


def _evaluation_time(value: datetime | None) -> datetime:
    evaluated = value or datetime.now(UTC)
    if evaluated.tzinfo is None or evaluated.utcoffset() is None:
        raise ValueError("macro evaluation time must include a timezone")
    return evaluated.astimezone(UTC)


def _select_series_as_of(
    snapshot: EvidenceSnapshot,
    evaluated_at: datetime,
) -> tuple[dict[str, MacroSeriesEvidence], dict[str, tuple[MacroObservationEvidence, ...]]]:
    candidates: dict[
        str,
        list[tuple[MacroSeriesEvidence, tuple[MacroObservationEvidence, ...]]],
    ] = defaultdict(list)
    for series in snapshot.macro_series:
        indicator = series.key.indicator_id
        if indicator not in _CANONICAL_IDS or not _series_matches_definition(series):
            continue
        history = _available_history(series, evaluated_at)
        if history:
            candidates[indicator].append((series, history))

    selected_series: dict[str, MacroSeriesEvidence] = {}
    histories: dict[str, tuple[MacroObservationEvidence, ...]] = {}
    for indicator, options in candidates.items():
        series, history = max(
            options,
            key=lambda item: (
                item[1][-1].reference_period.end_date,
                _SOURCE_PRIORITY.get(item[0].provenance.source_id, 1),
                item[1][-1].observation_version,
                item[0].series_id,
            ),
        )
        selected_series[indicator] = series
        histories[indicator] = history
    return selected_series, histories


def _series_matches_definition(series: MacroSeriesEvidence) -> bool:
    definition = get_macro_indicator(series.key.indicator_id)
    return all(
        (
            series.key.frequency == definition.expected_frequency,
            series.key.value_unit == definition.value_unit,
            series.key.value_scale == definition.value_scale,
            series.key.seasonal_adjustment == definition.seasonal_adjustment,
            series.key.country == definition.country,
            series.key.region == definition.region,
            series.key.currency == definition.currency,
            series.key.maturity == definition.maturity,
        )
    )


def _available_history(
    series: MacroSeriesEvidence,
    evaluated_at: datetime,
) -> tuple[MacroObservationEvidence, ...]:
    definition = get_macro_indicator(series.key.indicator_id)
    by_period: dict[str, MacroObservationEvidence] = {}
    for observation in series.observations:
        if (
            observation.reference_period.kind is not definition.reference_period_kind
            or not _observation_chronology_valid(observation)
        ):
            continue
        if not _observation_available_as_of(series.key.indicator_id, observation, evaluated_at):
            continue
        period_id = observation.reference_period.period_id
        existing = by_period.get(period_id)
        if existing is None or _version_rank(observation) > _version_rank(existing):
            by_period[period_id] = observation
    return tuple(
        sorted(
            by_period.values(),
            key=lambda item: (item.reference_period.end_date, item.observation_version),
        )
    )


def _observation_chronology_valid(observation: MacroObservationEvidence) -> bool:
    if observation.published_at is not None and observation.published_at > observation.received_at:
        return False
    if observation.vintage_at is not None and observation.vintage_at > observation.received_at:
        return False
    if (
        observation.published_at is not None
        and observation.vintage_at is not None
        and observation.vintage_at < observation.published_at
    ):
        return False
    if observation.observation_version > 1:
        return (
            observation.revision_of_observation_id is not None
            and observation.vintage_at is not None
        )
    return observation.revision_of_observation_id is None


def _version_rank(observation: MacroObservationEvidence) -> tuple[int, datetime, str]:
    timestamp = observation.vintage_at or observation.published_at or observation.received_at
    return observation.observation_version, timestamp, observation.observation_id


def _observation_available_as_of(
    indicator_id: str,
    observation: MacroObservationEvidence,
    evaluated_at: datetime,
) -> bool:
    if observation.received_at > evaluated_at:
        return False
    definition = get_macro_indicator(indicator_id)
    policy = get_freshness_policy(definition.freshness_policy_id)
    if observation.observation_version > 1:
        if observation.vintage_at is None or observation.vintage_at > evaluated_at:
            return False
        return observation.published_at is None or observation.published_at <= evaluated_at
    if policy.basis.value == "effective_at":
        return observation.effective_at is not None and observation.effective_at <= evaluated_at
    basis = observation.published_at
    if policy.basis.value == "published_or_observed_at":
        basis = basis or observation.observed_at
    return basis is not None and basis <= evaluated_at


def _freshness(
    indicator_id: str,
    observation: MacroObservationEvidence,
    evaluated_at: datetime,
) -> MacroFreshnessStatus:
    if not _observation_available_as_of(indicator_id, observation, evaluated_at):
        return MacroFreshnessStatus.UNKNOWN
    definition = get_macro_indicator(indicator_id)
    policy = get_freshness_policy(definition.freshness_policy_id)
    if observation.observation_version > 1:
        basis = observation.vintage_at
    elif policy.basis.value == "effective_at":
        basis = observation.effective_at
    elif policy.basis.value == "published_or_observed_at":
        basis = observation.published_at or observation.observed_at
    else:
        basis = observation.published_at
    if basis is None:
        return MacroFreshnessStatus.UNKNOWN
    age = evaluated_at - basis
    if age <= policy.fresh_for:
        return MacroFreshnessStatus.FRESH
    if age <= policy.aging_for:
        return MacroFreshnessStatus.AGING
    return MacroFreshnessStatus.STALE


def _status(freshness: MacroFreshnessStatus) -> MacroAssessmentStatus:
    if freshness in {MacroFreshnessStatus.FRESH, MacroFreshnessStatus.AGING}:
        return MacroAssessmentStatus.AVAILABLE
    if freshness is MacroFreshnessStatus.STALE:
        return MacroAssessmentStatus.STALE
    return MacroAssessmentStatus.INSUFFICIENT_DATA


def _combined_status(
    *,
    compatible: bool,
    freshness: tuple[MacroFreshnessStatus, ...],
    structurally_valid: bool = True,
) -> MacroAssessmentStatus:
    if not compatible or not structurally_valid:
        return MacroAssessmentStatus.INCOMPATIBLE
    if any(
        item in {MacroFreshnessStatus.UNKNOWN, MacroFreshnessStatus.NOT_AVAILABLE}
        for item in freshness
    ):
        return MacroAssessmentStatus.INSUFFICIENT_DATA
    if any(item is MacroFreshnessStatus.STALE for item in freshness):
        return MacroAssessmentStatus.STALE
    return MacroAssessmentStatus.AVAILABLE


def _direction(value) -> MacroDirection:
    if value > 0:
        return MacroDirection.RISING
    if value < 0:
        return MacroDirection.FALLING
    return MacroDirection.UNCHANGED


def _indicator_change(
    indicator_id: str,
    history: tuple[MacroObservationEvidence, ...],
    evaluated_at: datetime,
) -> MacroIndicatorChange:
    current = history[-1]
    freshness = _freshness(indicator_id, current, evaluated_at)
    if len(history) < 2:
        return MacroIndicatorChange(
            status=MacroAssessmentStatus.INSUFFICIENT_DATA,
            method_id="phase5.3.period_change",
            method_version="1",
            supporting_evidence_ids=(current.observation_id,),
            evidence_freshness=(freshness,),
            calculated_at=evaluated_at,
            missing_inputs=("previous_compatible_period",),
            limitations=("At least two chronological observations are required.",),
            confidence=0.0,
            indicator_id=indicator_id,
            current=current.value,
            current_period_end=current.reference_period.end_date,
        )
    previous = history[-2]
    if not economic_values_compatible(previous.value, current.value):
        return MacroIndicatorChange(
            status=MacroAssessmentStatus.INCOMPATIBLE,
            method_id="phase5.3.period_change",
            method_version="1",
            supporting_evidence_ids=(previous.observation_id, current.observation_id),
            evidence_freshness=(freshness,),
            calculated_at=evaluated_at,
            warnings=("Observation units or scales are incompatible.",),
            limitations=("No change was calculated.",),
            confidence=0.0,
            indicator_id=indicator_id,
            current=current.value,
            previous=previous.value,
            previous_period_end=previous.reference_period.end_date,
            current_period_end=current.reference_period.end_date,
        )
    status = _status(freshness)
    available = status is MacroAssessmentStatus.AVAILABLE
    change_value = current.value.value - previous.value.value
    return MacroIndicatorChange(
        status=status,
        method_id="phase5.3.period_change",
        method_version="1",
        supporting_evidence_ids=(previous.observation_id, current.observation_id),
        evidence_freshness=(freshness,),
        calculated_at=evaluated_at,
        missing_inputs=() if available else ("fresh_current_observation",),
        limitations=("Absolute period-to-period arithmetic change; no causal interpretation.",),
        confidence=0.78 if available else 0.0,
        indicator_id=indicator_id,
        current=current.value,
        previous=previous.value,
        change=(
            EconomicValue(value=change_value, unit=current.value.unit, scale=current.value.scale)
            if available
            else None
        ),
        direction=_direction(change_value) if available else None,
        previous_period_end=previous.reference_period.end_date,
        current_period_end=current.reference_period.end_date,
    )


def _event_available(event: EconomicEventEvidence, evaluated_at: datetime) -> bool:
    if event.received_at > evaluated_at:
        return False
    if event.release_status not in {EconomicReleaseStatus.RELEASED, EconomicReleaseStatus.REVISED}:
        return False
    return event.released_at is not None and event.released_at <= evaluated_at


def _event_identity_errors(event: EconomicEventEvidence) -> tuple[str, ...]:
    if event.indicator_id is None or event.indicator_id not in _CANONICAL_IDS:
        return ("unknown_indicator",)
    definition = get_macro_indicator(event.indicator_id)
    errors = []
    if definition.country is not None and event.country != definition.country:
        errors.append("country_mismatch")
    if definition.region is not None and event.region != definition.region:
        errors.append("region_mismatch")
    if definition.currency is not None and event.currency != definition.currency:
        errors.append("currency_mismatch")
    if event.released_at is not None and event.released_at > event.received_at:
        errors.append("release_after_retrieval")
    for label, provenance in (
        ("previous", event.previous_provenance),
        ("revised_previous", event.revised_previous_provenance),
        ("forecast", event.forecast_provenance),
        ("actual", event.actual_provenance),
    ):
        if provenance is not None and provenance.retrieved_at > event.received_at:
            errors.append(f"{label}_provenance_after_retrieval")
    return tuple(sorted(errors))


def _event_freshness(event: EconomicEventEvidence, evaluated_at: datetime) -> MacroFreshnessStatus:
    if not _event_available(event, evaluated_at):
        return MacroFreshnessStatus.UNKNOWN
    assert event.released_at is not None
    age_seconds = (evaluated_at - event.released_at).total_seconds()
    if age_seconds <= 2 * 86_400:
        return MacroFreshnessStatus.FRESH
    if age_seconds <= 7 * 86_400:
        return MacroFreshnessStatus.AGING
    return MacroFreshnessStatus.STALE


def _surprise(
    event: EconomicEventEvidence,
    evaluated_at: datetime,
) -> EconomicReleaseSurprise | None:
    if event.indicator_id not in _CANONICAL_IDS or not _event_available(event, evaluated_at):
        return None
    freshness = _event_freshness(event, evaluated_at)
    base = dict(
        method_id="phase5.3.release_surprise",
        method_version="1",
        supporting_evidence_ids=(event.event_id,),
        evidence_freshness=(freshness,),
        calculated_at=evaluated_at,
        event_id=event.event_id,
        indicator_id=event.indicator_id,
        actual=event.actual,
        forecast=event.forecast,
        released_at=event.released_at,
        retrieval_provider_id=event.provenance.source_id,
        original_publisher_id=event.provenance.origin_id,
    )
    identity_errors = _event_identity_errors(event)
    if identity_errors:
        return EconomicReleaseSurprise(
            **base,
            status=MacroAssessmentStatus.INVALID_INPUT,
            warnings=tuple(f"Release identity error: {item}." for item in identity_errors),
            limitations=("No surprise was calculated from invalid release identity or chronology.",),
            confidence=0.0,
        )
    missing = []
    if event.actual is None:
        missing.append("actual")
    if event.forecast is None:
        missing.append("forecast")
    if event.forecast is not None and event.forecast_provenance is None:
        missing.append("forecast_provenance")
    if missing:
        return EconomicReleaseSurprise(
            **base,
            status=MacroAssessmentStatus.INSUFFICIENT_DATA,
            missing_inputs=tuple(missing),
            limitations=("Missing release inputs are not replaced with zero or fallback values.",),
            confidence=0.0,
        )
    assert event.actual is not None and event.forecast is not None
    if not economic_values_compatible(event.actual, event.forecast):
        return EconomicReleaseSurprise(
            **base,
            status=MacroAssessmentStatus.INCOMPATIBLE,
            warnings=("Actual and forecast units or scales are incompatible.",),
            limitations=("No surprise was calculated.",),
            confidence=0.0,
        )
    status = _status(freshness)
    available = status is MacroAssessmentStatus.AVAILABLE
    delta = event.actual.value - event.forecast.value
    direction = (
        MacroDirection.ABOVE
        if delta > 0
        else MacroDirection.BELOW
        if delta < 0
        else MacroDirection.IN_LINE
    )
    return EconomicReleaseSurprise(
        **base,
        status=status,
        confidence=0.82 if available else 0.0,
        missing_inputs=() if available else ("fresh_release",),
        limitations=("Arithmetic surprise only; no market-reaction or causation claim.",),
        surprise=(
            EconomicValue(value=delta, unit=event.actual.unit, scale=event.actual.scale)
            if available
            else None
        ),
        direction=direction if available else None,
    )


def _revision(
    event: EconomicEventEvidence,
    evaluated_at: datetime,
) -> EconomicReleaseRevision | None:
    if event.indicator_id not in _CANONICAL_IDS or not _event_available(event, evaluated_at):
        return None
    if event.previous is None and event.revised_previous is None:
        return None
    freshness = _event_freshness(event, evaluated_at)
    base = dict(
        method_id="phase5.3.release_revision",
        method_version="1",
        supporting_evidence_ids=(event.event_id,),
        evidence_freshness=(freshness,),
        calculated_at=evaluated_at,
        event_id=event.event_id,
        indicator_id=event.indicator_id,
        previous=event.previous,
        revised_previous=event.revised_previous,
        released_at=event.released_at,
    )
    identity_errors = _event_identity_errors(event)
    if identity_errors:
        return EconomicReleaseRevision(
            **base,
            status=MacroAssessmentStatus.INVALID_INPUT,
            warnings=tuple(f"Release identity error: {item}." for item in identity_errors),
            limitations=("No revision was calculated from invalid release identity or chronology.",),
            confidence=0.0,
        )
    missing = []
    if event.previous is None:
        missing.append("previous")
    if event.revised_previous is None:
        missing.append("revised_previous")
    if missing:
        return EconomicReleaseRevision(
            **base,
            status=MacroAssessmentStatus.INSUFFICIENT_DATA,
            missing_inputs=tuple(missing),
            limitations=("Missing revision inputs are not fabricated.",),
            confidence=0.0,
        )
    assert event.previous is not None and event.revised_previous is not None
    if not economic_values_compatible(event.previous, event.revised_previous):
        return EconomicReleaseRevision(
            **base,
            status=MacroAssessmentStatus.INCOMPATIBLE,
            warnings=("Previous and revised values use incompatible units or scales.",),
            limitations=("No revision magnitude was calculated.",),
            confidence=0.0,
        )
    status = _status(freshness)
    available = status is MacroAssessmentStatus.AVAILABLE
    delta = event.revised_previous.value - event.previous.value
    return EconomicReleaseRevision(
        **base,
        status=status,
        confidence=0.84 if available else 0.0,
        missing_inputs=() if available else ("fresh_revision",),
        limitations=("Arithmetic revision only; no causal interpretation.",),
        revision=(
            EconomicValue(value=delta, unit=event.previous.unit, scale=event.previous.scale)
            if available
            else None
        ),
        direction=_direction(delta) if available else None,
    )


def _policy_rate_differentials(
    latest: dict[str, MacroObservationEvidence],
    evaluated_at: datetime,
) -> tuple[PolicyRateDifferential, ...]:
    rates = {
        currency: latest[indicator]
        for currency, indicator in _POLICY_RATE_BY_CURRENCY.items()
        if indicator in latest
    }
    order = ("USD", "EUR", "GBP", "JPY")
    results = []
    for index, base_currency in enumerate(order):
        for quote_currency in order[index + 1 :]:
            if base_currency not in rates or quote_currency not in rates:
                continue
            base = rates[base_currency]
            quote = rates[quote_currency]
            base_indicator = _POLICY_RATE_BY_CURRENCY[base_currency]
            quote_indicator = _POLICY_RATE_BY_CURRENCY[quote_currency]
            freshness = (
                _freshness(base_indicator, base, evaluated_at),
                _freshness(quote_indicator, quote, evaluated_at),
            )
            compatible = economic_values_compatible(base.value, quote.value)
            status = _combined_status(compatible=compatible, freshness=freshness)
            available = status is MacroAssessmentStatus.AVAILABLE
            delta = base.value.value - quote.value.value
            results.append(
                PolicyRateDifferential(
                    status=status,
                    method_id="phase5.3.policy_rate_differential",
                    method_version="1",
                    supporting_evidence_ids=(base.observation_id, quote.observation_id),
                    evidence_freshness=freshness,
                    calculated_at=evaluated_at,
                    missing_inputs=() if available else ("compatible_fresh_policy_rates",),
                    warnings=() if compatible else ("Policy-rate units or scales are incompatible.",),
                    limitations=("Arithmetic effective-rate differential; no FX direction claim.",),
                    confidence=0.82 if available else 0.0,
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    base_rate=base.value,
                    quote_rate=quote.value,
                    differential=(
                        EconomicValue(value=delta, unit=base.value.unit, scale=base.value.scale)
                        if available
                        else None
                    ),
                    base_effective_at=base.effective_at,
                    quote_effective_at=quote.effective_at,
                )
            )
    return tuple(results)


def _yield_spreads(
    latest: dict[str, MacroObservationEvidence],
    evaluated_at: datetime,
) -> tuple[YieldCurveSpread, ...]:
    results = []
    for short_maturity, long_maturity in (("3M", "10Y"), ("2Y", "10Y")):
        short_id = f"us_nominal_treasury_yield_{short_maturity.lower()}"
        long_id = f"us_nominal_treasury_yield_{long_maturity.lower()}"
        short = latest.get(short_id)
        long = latest.get(long_id)
        if short is None or long is None:
            continue
        freshness = (_freshness(short_id, short, evaluated_at), _freshness(long_id, long, evaluated_at))
        compatible = economic_values_compatible(short.value, long.value)
        same_date = short.reference_period.end_date == long.reference_period.end_date
        status = _combined_status(
            compatible=compatible,
            freshness=freshness,
            structurally_valid=same_date,
        )
        available = status is MacroAssessmentStatus.AVAILABLE
        delta = long.value.value - short.value.value
        warnings = []
        if not same_date:
            warnings.append("Yield observations are from different dates.")
        if not compatible:
            warnings.append("Yield units or scales are incompatible.")
        results.append(
            YieldCurveSpread(
                status=status,
                method_id="phase5.3.yield_curve_spread",
                method_version="1",
                supporting_evidence_ids=(short.observation_id, long.observation_id),
                evidence_freshness=freshness,
                calculated_at=evaluated_at,
                missing_inputs=() if available else ("same_date_compatible_fresh_yields",),
                warnings=tuple(warnings),
                limitations=("Long yield minus short yield; no recession or price-direction claim.",),
                confidence=0.86 if available else 0.0,
                short_maturity=short_maturity,
                long_maturity=long_maturity,
                short_yield=short.value,
                long_yield=long.value,
                spread=(
                    EconomicValue(value=delta, unit=long.value.unit, scale=long.value.scale)
                    if available
                    else None
                ),
                short_observed_at=short.reference_period.end_date,
                long_observed_at=long.reference_period.end_date,
            )
        )
    return tuple(results)


def _real_yields(
    latest: dict[str, MacroObservationEvidence],
    evaluated_at: datetime,
) -> tuple[RealYieldContext, ...]:
    results = []
    for maturity in ("5y", "10y", "30y"):
        indicator = f"us_real_treasury_yield_{maturity}"
        observation = latest.get(indicator)
        if observation is None:
            continue
        freshness = _freshness(indicator, observation, evaluated_at)
        status = _status(freshness)
        available = status is MacroAssessmentStatus.AVAILABLE
        results.append(
            RealYieldContext(
                status=status,
                method_id="phase5.3.direct_real_yield",
                method_version="1",
                supporting_evidence_ids=(observation.observation_id,),
                evidence_freshness=(freshness,),
                calculated_at=evaluated_at,
                missing_inputs=() if available else ("fresh_direct_real_yield",),
                limitations=("Directly observed official real yield; no synthetic subtraction.",),
                confidence=0.88 if available else 0.0,
                maturity=maturity.upper(),
                real_yield=observation.value if available else None,
                nominal_yield=None,
                inflation_measure=None,
            )
        )
    return tuple(results)


def _conflicting_evidence(
    snapshot: EvidenceSnapshot,
    evaluated_at: datetime,
) -> dict[str, tuple[str, ...]]:
    by_indicator: dict[str, list[MacroObservationEvidence]] = defaultdict(list)
    for series in snapshot.macro_series:
        if series.key.indicator_id not in _CANONICAL_IDS or not _series_matches_definition(series):
            continue
        history = _available_history(series, evaluated_at)
        if history:
            by_indicator[series.key.indicator_id].append(history[-1])
    conflicts = {}
    for indicator, observations in by_indicator.items():
        latest_period = max(item.reference_period.end_date for item in observations)
        comparable = [item for item in observations if item.reference_period.end_date == latest_period]
        identities = {(item.value.value, item.value.unit, item.value.scale) for item in comparable}
        if len(identities) > 1:
            conflicts[indicator] = tuple(sorted(item.observation_id for item in comparable))
    return conflicts


def _observation_summaries(
    snapshot: EvidenceSnapshot,
    evaluated_at: datetime,
) -> tuple[MacroObservationSummary, ...]:
    values = []
    for series in snapshot.macro_series:
        indicator = series.key.indicator_id
        if indicator not in _CANONICAL_IDS or not _series_matches_definition(series):
            continue
        history = _available_history(series, evaluated_at)
        if not history:
            continue
        observation = history[-1]
        values.append(
            MacroObservationSummary(
                observation_id=observation.observation_id,
                indicator_id=indicator,
                value=observation.value,
                reference_period_start=observation.reference_period.start_date,
                reference_period_end=observation.reference_period.end_date,
                freshness=_freshness(indicator, observation, evaluated_at),
                published_at=observation.published_at,
                effective_at=observation.effective_at,
                vintage_at=observation.vintage_at,
                retrieved_at=observation.received_at,
                retrieval_provider_id=series.provenance.source_id,
                retrieval_provider_name=series.provenance.source_name,
                original_publisher_id=series.provenance.origin_id,
                original_publisher_name=series.provenance.origin_name,
            )
        )
    return tuple(sorted(values, key=lambda item: (item.indicator_id, item.observation_id)))


def _cross_market_context(
    subject: str,
    latest: dict[str, MacroObservationEvidence],
    evaluated_at: datetime,
) -> tuple[CrossMarketObservation, ...]:
    dxy = latest.get("us_dollar_index_context")
    if dxy is None:
        return ()
    freshness = _freshness("us_dollar_index_context", dxy, evaluated_at)
    if freshness not in {MacroFreshnessStatus.FRESH, MacroFreshnessStatus.AGING}:
        return ()
    return (
        CrossMarketObservation(
            status=MacroAssessmentStatus.AVAILABLE,
            method_id="phase5.3.cross_market_context",
            method_version="1",
            supporting_evidence_ids=(dxy.observation_id,),
            evidence_freshness=(freshness,),
            calculated_at=evaluated_at,
            limitations=("Observed USD index context only; no causal or directional relationship calculated.",),
            confidence=0.65,
            primary_instrument_id=subject,
            context_instrument_id="us_dollar_index_context",
            observation=f"Observed USD index context: {dxy.value.value} {dxy.value.unit.value}.",
        ),
    )


def _instrument_assessment(
    *,
    subject: str,
    latest: dict[str, MacroObservationEvidence],
    changes: tuple[MacroIndicatorChange, ...],
    surprises: tuple[EconomicReleaseSurprise, ...],
    differentials: tuple[PolicyRateDifferential, ...],
    spreads: tuple[YieldCurveSpread, ...],
    real_yields: tuple[RealYieldContext, ...],
    conflicts: dict[str, tuple[str, ...]],
    evaluated_at: datetime,
) -> InstrumentSpecificMacroAssessment:
    instrument_class = _instrument_class(subject)
    policy = next(item for item in INSTRUMENT_MACRO_POLICIES if item.instrument_class is instrument_class)
    available_changes = {
        item.indicator_id: item
        for item in changes
        if item.status is MacroAssessmentStatus.AVAILABLE
    }
    present_factors: set[MacroFactor] = set()
    supporting: set[str] = set()
    facts = []
    implications = []
    inflation_changes = [item for key, item in available_changes.items() if key in _INFLATION_IDS]
    growth_changes = [item for key, item in available_changes.items() if key in _GROWTH_IDS]
    if inflation_changes:
        present_factors.add(MacroFactor.INFLATION)
    fed = latest.get("fed_policy_rate")
    if fed is not None and _freshness("fed_policy_rate", fed, evaluated_at) in {
        MacroFreshnessStatus.FRESH,
        MacroFreshnessStatus.AGING,
    }:
        present_factors.add(MacroFactor.POLICY_RATES)
        supporting.add(fed.observation_id)
    if any(item.status is MacroAssessmentStatus.AVAILABLE for item in real_yields):
        present_factors.add(MacroFactor.REAL_YIELDS)
    if any(item.status is MacroAssessmentStatus.AVAILABLE for item in spreads):
        present_factors.add(MacroFactor.NOMINAL_YIELDS)
    dxy = latest.get("us_dollar_index_context")
    if dxy is not None and _freshness("us_dollar_index_context", dxy, evaluated_at) in {
        MacroFreshnessStatus.FRESH,
        MacroFreshnessStatus.AGING,
    }:
        present_factors.add(MacroFactor.US_DOLLAR)
        supporting.add(dxy.observation_id)
    if growth_changes:
        present_factors.add(MacroFactor.INDUSTRIAL_GROWTH)

    for item in (*inflation_changes, *growth_changes):
        supporting.update(item.supporting_evidence_ids)
        facts.append(
            f"{get_macro_indicator(item.indicator_id).display_name} was "
            f"{item.direction.value if item.direction else 'unchanged'} between "
            f"{item.previous_period_end} and {item.current_period_end}."
        )
    for item in real_yields:
        if item.status is MacroAssessmentStatus.AVAILABLE and item.real_yield is not None:
            supporting.update(item.supporting_evidence_ids)
            facts.append(
                f"Direct {item.maturity} real yield evidence is "
                f"{item.real_yield.value} {item.real_yield.unit.value}."
            )
    for item in spreads:
        if item.status is MacroAssessmentStatus.AVAILABLE and item.spread is not None:
            supporting.update(item.supporting_evidence_ids)
            facts.append(
                f"{item.short_maturity}-{item.long_maturity} spread is "
                f"{item.spread.value} {item.spread.unit.value}."
            )

    pair = _currency_pair(subject)
    if instrument_class is InstrumentMacroClass.FOREX and pair is not None:
        base, quote = pair
        base_id = _POLICY_RATE_BY_CURRENCY.get(base)
        quote_id = _POLICY_RATE_BY_CURRENCY.get(quote)
        if base_id and base_id in latest and _freshness(base_id, latest[base_id], evaluated_at) in {
            MacroFreshnessStatus.FRESH,
            MacroFreshnessStatus.AGING,
        }:
            present_factors.add(MacroFactor.BASE_CURRENCY_POLICY)
            supporting.add(latest[base_id].observation_id)
        if quote_id and quote_id in latest and _freshness(quote_id, latest[quote_id], evaluated_at) in {
            MacroFreshnessStatus.FRESH,
            MacroFreshnessStatus.AGING,
        }:
            present_factors.add(MacroFactor.QUOTE_CURRENCY_POLICY)
            supporting.add(latest[quote_id].observation_id)
        differential = next(
            (
                item
                for item in differentials
                if item.status is MacroAssessmentStatus.AVAILABLE
                and {item.base_currency, item.quote_currency} == {base, quote}
            ),
            None,
        )
        if differential is not None:
            present_factors.add(MacroFactor.RATE_DIFFERENTIAL)
            supporting.update(differential.supporting_evidence_ids)
            implications.append(
                f"The observed {differential.base_currency}-{differential.quote_currency} "
                "policy differential is rate context only and does not predict the currency pair."
            )
        release_currencies = {
            get_macro_indicator(item.indicator_id).currency
            for item in surprises
            if item.status is MacroAssessmentStatus.AVAILABLE and item.indicator_id is not None
        }
        if base in release_currencies and quote in release_currencies:
            present_factors.add(MacroFactor.CURRENCY_SPECIFIC_RELEASES)

    if instrument_class is InstrumentMacroClass.GOLD and inflation_changes:
        implications.append(
            "Observed inflation changes may matter for Gold only through policy, real-yield, "
            "USD, and risk responses that require separate evidence."
        )
    if instrument_class is InstrumentMacroClass.SILVER and growth_changes:
        implications.append(
            "Observed growth changes are industrial-demand context for Silver, not proof of price direction."
        )

    conflicting_ids = tuple(sorted({value for ids in conflicts.values() for value in ids}))
    missing_factors = tuple(
        sorted(f"factor:{factor.value}" for factor in set(policy.required_factors) - present_factors)
    )
    status = (
        MacroAssessmentStatus.AVAILABLE
        if not missing_factors and supporting
        else MacroAssessmentStatus.INSUFFICIENT_DATA
    )
    freshness = tuple(
        sorted(
            {
                _freshness(indicator, observation, evaluated_at)
                for indicator, observation in latest.items()
                if observation.observation_id in supporting
            },
            key=lambda item: item.value,
        )
    )
    if status is MacroAssessmentStatus.AVAILABLE and any(
        item not in {MacroFreshnessStatus.FRESH, MacroFreshnessStatus.AGING}
        for item in freshness
    ):
        status = MacroAssessmentStatus.INSUFFICIENT_DATA
        missing_factors = (*missing_factors, "fresh_supporting_evidence")
    warning = (
        "Conflicting source observations are present."
        if conflicting_ids
        else "No automatic buy/sell signal is produced."
    )
    return InstrumentSpecificMacroAssessment(
        status=status,
        method_id="phase5.3.instrument_macro_assessment",
        method_version="1",
        supporting_evidence_ids=tuple(sorted(supporting)),
        evidence_freshness=freshness,
        calculated_at=evaluated_at,
        missing_inputs=missing_factors,
        warnings=(warning,),
        limitations=(
            "Assessment is conditional and cannot establish price causation.",
            "No provider retrieval is performed during analysis.",
        ),
        confidence=(min(0.8, len(present_factors) / len(policy.required_factors)) if supporting else 0.0),
        instrument_id=subject,
        instrument_class=instrument_class,
        assessed_factors=tuple(sorted(present_factors, key=lambda item: item.value)),
        verified_facts=tuple(sorted(set(facts))),
        interpretations=(),
        conditional_implications=tuple(sorted(set(implications))),
        conflicting_evidence_ids=conflicting_ids,
    )


def _instrument_class(subject: str) -> InstrumentMacroClass:
    normalized = subject.upper().replace("/", "").replace("-", "")
    if "XAU" in normalized or "GOLD" in normalized:
        return InstrumentMacroClass.GOLD
    if "XAG" in normalized or "SILVER" in normalized:
        return InstrumentMacroClass.SILVER
    return InstrumentMacroClass.FOREX


def _currency_pair(subject: str) -> tuple[str, str] | None:
    normalized = "".join(character for character in subject.upper() if character.isalpha())
    if len(normalized) != 6:
        return None
    return normalized[:3], normalized[3:]


def default_provider_diagnostics() -> tuple[MacroProviderDiagnostic, ...]:
    values = []
    for provider_id in MacroProviderId:
        source_type = (
            MacroSourceType.CENTRAL_BANK
            if provider_id is MacroProviderId.FEDERAL_RESERVE
            else MacroSourceType.COMMERCIAL_CONSENSUS
            if provider_id is MacroProviderId.TRADING_ECONOMICS
            else MacroSourceType.OFFICIAL_OBSERVATION
        )
        values.append(
            MacroProviderDiagnostic(
                provider_id=provider_id.value,
                source_type=source_type,
                status=MacroProviderStatus.NOT_CONFIGURED,
                request_count=0,
                reason_codes=("PROVIDER_NOT_CONFIGURED",),
                error_category="configuration",
                failure_stage="configuration",
                request_sent=False,
            )
        )
    return tuple(values)
