"""Provider-neutral construction helpers for validated macro observations."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.models.economic_events import EconomicValue
from app.models.events import DataDomain, SourceTransport
from app.models.macro_providers import MacroProviderMapping
from app.models.macro_series import (
    MacroFrequency,
    MacroReferencePeriod,
    MacroReferencePeriodKind,
    MacroSeriesKey,
    MacroSeriesSource,
    NormalizedMacroObservation,
)
from app.models.economic_events import NormalizedEconomicEvent
from app.services.macro_policies import get_macro_indicator


def parse_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("macro value is missing or invalid")
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError("macro value is not numeric") from None
    if not parsed.is_finite():
        raise ValueError("macro value must be finite")
    return parsed


def period_for_date(
    value: date,
    kind: MacroReferencePeriodKind,
) -> MacroReferencePeriod:
    if kind in {MacroReferencePeriodKind.DATE, MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE}:
        return MacroReferencePeriod(kind=kind, start_date=value, end_date=value)
    if kind is MacroReferencePeriodKind.MONTH:
        end = date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])
        return MacroReferencePeriod(kind=kind, start_date=date(value.year, value.month, 1), end_date=end)
    if kind is MacroReferencePeriodKind.QUARTER:
        start_month = ((value.month - 1) // 3) * 3 + 1
        end_month = start_month + 2
        end = date(value.year, end_month, calendar.monthrange(value.year, end_month)[1])
        return MacroReferencePeriod(kind=kind, start_date=date(value.year, start_month, 1), end_date=end)
    raise ValueError(f"unsupported macro reference period kind: {kind.value}")


def observation_from_mapping(
    mapping: MacroProviderMapping,
    *,
    provider_name: str,
    reference_date: date,
    raw_value: object,
    retrieved_at: datetime,
    observed_at: datetime | None = None,
    effective_at: datetime | None = None,
    published_at: datetime | None = None,
    vintage_at: datetime | None = None,
    observation_version: int = 1,
    revision_of_observation_id: str | None = None,
) -> NormalizedMacroObservation:
    definition = get_macro_indicator(mapping.indicator_id)
    if mapping.provider_series_id is None:
        raise ValueError("provider mapping has no verified series identity")
    source = MacroSeriesSource(
        source_id=mapping.provider_id.value,
        display_name=provider_name,
        transport=SourceTransport.API,
        domain=DataDomain.ECONOMIC_DATA,
        origin_id=mapping.original_publisher_id,
        origin_name=mapping.original_publisher_name,
    )
    key = MacroSeriesKey(
        source_id=mapping.provider_id.value,
        source_series_id=mapping.provider_series_id,
        indicator_id=definition.indicator_id,
        country=definition.country,
        region=definition.region,
        currency=definition.currency,
        maturity=definition.maturity,
        frequency=definition.expected_frequency,
        seasonal_adjustment=definition.seasonal_adjustment,
        value_unit=definition.value_unit,
        value_scale=definition.value_scale,
    )
    if definition.expected_frequency not in {
        MacroFrequency.DAILY,
        MacroFrequency.MONTHLY,
        MacroFrequency.QUARTERLY,
        MacroFrequency.POLICY_RATE,
    }:
        raise ValueError("provider observation frequency is unsupported")
    return NormalizedMacroObservation(
        source=source,
        series_key=key,
        reference_period=period_for_date(reference_date, definition.reference_period_kind),
        value=EconomicValue(
            value=parse_decimal(raw_value),
            unit=definition.value_unit,
            scale=definition.value_scale,
        ),
        observation_version=observation_version,
        revision_of_observation_id=revision_of_observation_id,
        observed_at=observed_at,
        effective_at=effective_at,
        published_at=published_at,
        vintage_at=vintage_at,
        received_at=retrieved_at.astimezone(UTC),
    )


def deduplicate_observations(
    values: list[NormalizedMacroObservation],
) -> tuple[NormalizedMacroObservation, ...]:
    """Suppress exact duplicates and reject identity collisions deterministically."""
    retained: dict[str, NormalizedMacroObservation] = {}
    for value in values:
        existing = retained.get(value.observation_id)
        if existing is not None and existing.fingerprint != value.fingerprint:
            raise ValueError("conflicting normalized macro observation identity")
        retained[value.observation_id] = value
    return tuple(sorted(retained.values(), key=lambda item: item.observation_id))


def deduplicate_events(
    values: tuple[NormalizedEconomicEvent, ...] | list[NormalizedEconomicEvent],
) -> tuple[NormalizedEconomicEvent, ...]:
    """Suppress exact provider duplicates without discarding conflicting versions."""
    retained: dict[str, NormalizedEconomicEvent] = {}
    for value in values:
        existing = retained.get(value.event_id)
        if existing is not None and existing.fingerprint != value.fingerprint:
            raise ValueError("conflicting normalized economic-event identity")
        retained[value.event_id] = value
    return tuple(sorted(retained.values(), key=lambda item: item.event_id))
