"""Immutable provider-neutral contracts for observed macroeconomic series.

Macro observations are persistent values indexed by a reference period and,
when supplied, a source revision/vintage.  They are intentionally separate
from OHLC market bars and discrete economic-event lifecycles.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.schemas import APIModel


class MacroSeriesModel(APIModel):
    """Frozen base for provider-neutral macro-series contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MacroFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    POLICY_RATE = "policy_rate"
    IRREGULAR = "irregular"


class SeasonalAdjustment(str, Enum):
    SEASONALLY_ADJUSTED = "seasonally_adjusted"
    NOT_SEASONALLY_ADJUSTED = "not_seasonally_adjusted"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class MacroReferencePeriodKind(str, Enum):
    DATE = "date"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    INTERVAL = "interval"
    POLICY_EFFECTIVE_DATE = "policy_effective_date"
    IRREGULAR = "irregular"


class MacroSeriesSource(MacroSeriesModel):
    """Immutable identity for the connector that supplied a macro series."""

    source_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    transport: SourceTransport
    domain: DataDomain
    origin_id: str | None = Field(default=None, max_length=80)
    origin_name: str | None = Field(default=None, max_length=120)

    @field_validator("source_id", "display_name", "origin_id", "origin_name", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_source(self) -> "MacroSeriesSource":
        if self.domain is not DataDomain.ECONOMIC_DATA:
            raise ValueError("macro series must use the economic_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("macro source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class MacroReferencePeriod(MacroSeriesModel):
    """Calendar/reference semantics without market-bar timing assumptions."""

    kind: MacroReferencePeriodKind
    start_date: date
    end_date: date
    label: str | None = Field(default=None, max_length=120)

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_period(self) -> "MacroReferencePeriod":
        if self.end_date < self.start_date:
            raise ValueError("reference-period end_date cannot precede start_date")
        if self.kind in {
            MacroReferencePeriodKind.DATE,
            MacroReferencePeriodKind.POLICY_EFFECTIVE_DATE,
        } and self.start_date != self.end_date:
            raise ValueError("date-based reference periods require matching start and end dates")
        if self.kind is MacroReferencePeriodKind.WEEK and (
            self.end_date - self.start_date
        ).days != 6:
            raise ValueError("weekly reference periods must contain exactly seven calendar days")
        if self.kind is MacroReferencePeriodKind.MONTH and not _is_calendar_month(
            self.start_date,
            self.end_date,
        ):
            raise ValueError("monthly reference periods must cover one complete calendar month")
        if self.kind is MacroReferencePeriodKind.QUARTER and not _is_calendar_quarter(
            self.start_date,
            self.end_date,
        ):
            raise ValueError("quarterly reference periods must cover one complete calendar quarter")
        return self

    @computed_field(return_type=str)
    @property
    def period_id(self) -> str:
        return _stable_id(
            "macro-period",
            self.kind.value,
            self.start_date.isoformat(),
            self.end_date.isoformat(),
        )


class MacroSeriesKey(MacroSeriesModel):
    """Stable source-scoped identity for one observed macroeconomic series."""

    source_id: str = Field(min_length=1, max_length=80)
    source_series_id: str = Field(min_length=1, max_length=160)
    indicator_id: str = Field(min_length=1, max_length=160)
    country: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=120)
    currency: str | None = Field(default=None, max_length=12)
    maturity: str | None = Field(default=None, max_length=40)
    frequency: MacroFrequency
    seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.UNKNOWN
    value_unit: EconomicValueUnit
    value_scale: EconomicValueScale = EconomicValueScale.ONE

    @field_validator("source_id", "source_series_id", "indicator_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("macro-series identity values cannot be blank")
            return normalized
        return value

    @field_validator("country", "region", "currency", "maturity", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("country", "currency")
    @classmethod
    def normalize_codes(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def validate_geography(self) -> "MacroSeriesKey":
        if self.country is None and self.region is None:
            raise ValueError("a macro series requires country or region")
        return self

    @computed_field(return_type=str)
    @property
    def series_id(self) -> str:
        return _stable_id(
            "macro-series",
            self.source_id,
            self.source_series_id,
            self.indicator_id,
            self.country or "",
            self.region or "",
            self.currency or "",
            self.maturity or "",
            self.frequency.value,
            self.seasonal_adjustment.value,
            self.value_unit.value,
            self.value_scale.value,
        )


class NormalizedMacroObservation(MacroSeriesModel):
    """One immutable, normalized macro-series observation or revision.

    ``received_at`` is operational provenance only and deliberately excluded
    from the content fingerprint, so duplicate handling is not affected by
    local ingestion timing.
    """

    source: MacroSeriesSource
    series_key: MacroSeriesKey
    reference_period: MacroReferencePeriod
    value: EconomicValue
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    published_at: datetime | None = None
    vintage_at: datetime | None = None
    received_at: datetime

    @field_validator("revision_of_observation_id", mode="before")
    @classmethod
    def normalize_revision_link(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator(
        "observed_at",
        "effective_at",
        "published_at",
        "vintage_at",
        "received_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro-observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_observation(self) -> "NormalizedMacroObservation":
        if self.source.source_id != self.series_key.source_id:
            raise ValueError("macro observation source_id must match the series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("macro observation value unit must match the series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("macro observation value scale must match the series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised observations require revision_of_observation_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        payload = {
            "source": {
                "source_id": self.source.source_id,
                "display_name": self.source.display_name,
                "transport": self.source.transport.value,
                "domain": self.source.domain.value,
                "origin_id": self.source.origin_id,
                "origin_name": self.source.origin_name,
            },
            "series_key": _canonical_series_key(self.series_key),
            "reference_period": {
                "kind": self.reference_period.kind.value,
                "start_date": self.reference_period.start_date.isoformat(),
                "end_date": self.reference_period.end_date.isoformat(),
                "label": self.reference_period.label,
            },
            "value": _canonical_value(self.value),
            "observation_version": self.observation_version,
            "revision_of_observation_id": self.revision_of_observation_id,
            "observed_at": _optional_timestamp(self.observed_at),
            "effective_at": _optional_timestamp(self.effective_at),
            "published_at": _optional_timestamp(self.published_at),
            "vintage_at": _optional_timestamp(self.vintage_at),
        }
        return _fingerprint(payload)

    @computed_field(return_type=str)
    @property
    def observation_id(self) -> str:
        return "macro-observation-" + _fingerprint(
            {
                "series_id": self.series_key.series_id,
                "reference_period_id": self.reference_period.period_id,
                "observation_version": self.observation_version,
                "fingerprint": self.fingerprint,
            }
        )


def _is_calendar_month(start: date, end: date) -> bool:
    if start.day != 1 or start.year != end.year or start.month != end.month:
        return False
    next_month = date(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return end == date.fromordinal(next_month.toordinal() - 1)


def _is_calendar_quarter(start: date, end: date) -> bool:
    if start.month not in {1, 4, 7, 10} or start.day != 1:
        return False
    expected_end_month = start.month + 2
    next_month = date(
        start.year + (expected_end_month == 12),
        (expected_end_month % 12) + 1,
        1,
    )
    return end == date.fromordinal(next_month.toordinal() - 1)


def _canonical_series_key(key: MacroSeriesKey) -> dict[str, str | None]:
    return {
        "source_id": key.source_id,
        "source_series_id": key.source_series_id,
        "indicator_id": key.indicator_id,
        "country": key.country,
        "region": key.region,
        "currency": key.currency,
        "maturity": key.maturity,
        "frequency": key.frequency.value,
        "seasonal_adjustment": key.seasonal_adjustment.value,
        "value_unit": key.value_unit.value,
        "value_scale": key.value_scale.value,
    }


def _canonical_value(value: EconomicValue) -> dict[str, str]:
    normalized = format(value.value.normalize(), "f")
    if normalized == "-0":
        normalized = "0"
    return {
        "value": normalized,
        "unit": value.unit.value,
        "scale": value.scale.value,
    }


def _optional_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()}"


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
