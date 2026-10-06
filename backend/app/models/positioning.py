"""Immutable provider-neutral contracts for positioning and participation data.

Positioning observations describe reported holdings, flows, participation, or
open-interest measures.  They are intentionally distinct from market bars,
economic releases, and macroeconomic series: a reported participant measure
cannot be inferred from price data and has its own reporting-period and
revision semantics.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
import json
import re

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.schemas import APIModel


class PositioningModel(APIModel):
    """Frozen base for provider-neutral positioning contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PositioningFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    IRREGULAR = "irregular"


class PositioningReferencePeriodKind(str, Enum):
    DATE = "date"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    INTERVAL = "interval"
    IRREGULAR = "irregular"


class ParticipantCategory(str, Enum):
    TOTAL = "total"
    COMMERCIAL = "commercial"
    NON_COMMERCIAL = "non_commercial"
    DEALER_INTERMEDIARY = "dealer_intermediary"
    ASSET_MANAGER = "asset_manager"
    LEVERAGED_FUND = "leveraged_fund"
    OTHER_REPORTABLE = "other_reportable"
    NON_REPORTABLE = "non_reportable"
    UNKNOWN = "unknown"


class PositioningMeasure(str, Enum):
    NET_POSITION = "net_position"
    LONG_POSITION = "long_position"
    SHORT_POSITION = "short_position"
    OPEN_INTEREST = "open_interest"
    HOLDINGS = "holdings"
    NET_FLOW = "net_flow"
    GROSS_INFLOW = "gross_inflow"
    GROSS_OUTFLOW = "gross_outflow"
    OWNERSHIP_FRACTION = "ownership_fraction"
    PARTICIPATION_RATE = "participation_rate"
    UNKNOWN = "unknown"


class PositioningSource(PositioningModel):
    """Immutable identity for a connector that supplied positioning data."""

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
    def validate_source(self) -> "PositioningSource":
        if self.domain is not DataDomain.POSITIONING_DATA:
            raise ValueError("positioning sources must use the positioning_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("positioning source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class PositioningReferencePeriod(PositioningModel):
    """Reported-period semantics without market-bar timing assumptions."""

    kind: PositioningReferencePeriodKind
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
    def validate_period(self) -> "PositioningReferencePeriod":
        if self.end_date < self.start_date:
            raise ValueError("reference-period end_date cannot precede start_date")
        if self.kind is PositioningReferencePeriodKind.DATE and self.start_date != self.end_date:
            raise ValueError("date reference periods require matching start and end dates")
        if self.kind is PositioningReferencePeriodKind.WEEK and (
            self.end_date - self.start_date
        ).days != 6:
            raise ValueError("weekly reference periods must contain exactly seven calendar days")
        if self.kind is PositioningReferencePeriodKind.MONTH and not _is_calendar_month(
            self.start_date,
            self.end_date,
        ):
            raise ValueError("monthly reference periods must cover one complete calendar month")
        if self.kind is PositioningReferencePeriodKind.QUARTER and not _is_calendar_quarter(
            self.start_date,
            self.end_date,
        ):
            raise ValueError("quarterly reference periods must cover one complete calendar quarter")
        return self

    @computed_field(return_type=str)
    @property
    def period_id(self) -> str:
        return _stable_id(
            "positioning-period",
            self.kind.value,
            self.start_date.isoformat(),
            self.end_date.isoformat(),
        )


class PositioningSeriesKey(PositioningModel):
    """Stable source-scoped identity for one reported positioning measure."""

    source_id: str = Field(min_length=1, max_length=80)
    source_series_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=160)
    symbol: str | None = Field(default=None, max_length=40)
    asset_class: AssetClass = AssetClass.UNKNOWN
    venue: str | None = Field(default=None, max_length=80)
    participant_category: ParticipantCategory
    measure: PositioningMeasure
    frequency: PositioningFrequency
    value_unit: EconomicValueUnit
    value_scale: EconomicValueScale = EconomicValueScale.ONE

    @field_validator("source_id", "source_series_id", "instrument_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("positioning-series identity values cannot be blank")
            return normalized
        return value

    @field_validator("symbol", "venue", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            return normalized or None
        return value

    @field_validator("symbol")
    @classmethod
    def canonicalize_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        canonical = re.sub(r"[^A-Z0-9]", "", value.rsplit(":", maxsplit=1)[-1])
        if not canonical:
            raise ValueError("symbol must contain letters or numbers")
        return canonical

    @computed_field(return_type=str)
    @property
    def series_id(self) -> str:
        return _stable_id(
            "positioning-series",
            self.source_id,
            self.source_series_id,
            self.instrument_id,
            self.symbol or "",
            self.asset_class.value,
            self.venue or "",
            self.participant_category.value,
            self.measure.value,
            self.frequency.value,
            self.value_unit.value,
            self.value_scale.value,
        )


class NormalizedPositioningObservation(PositioningModel):
    """One immutable reported positioning observation or revision.

    ``received_at`` is operational provenance and deliberately excluded from
    content identity, so retries cannot create distinct observations.
    """

    source: PositioningSource
    series_key: PositioningSeriesKey
    reference_period: PositioningReferencePeriod
    value: EconomicValue
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    observed_at: datetime | None = None
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

    @field_validator("observed_at", "published_at", "vintage_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("positioning-observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_observation(self) -> "NormalizedPositioningObservation":
        if self.source.source_id != self.series_key.source_id:
            raise ValueError("positioning observation source_id must match the series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("positioning observation value unit must match the series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("positioning observation value scale must match the series key")
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
            "published_at": _optional_timestamp(self.published_at),
            "vintage_at": _optional_timestamp(self.vintage_at),
        }
        return _fingerprint(payload)

    @computed_field(return_type=str)
    @property
    def observation_id(self) -> str:
        return "positioning-observation-" + _fingerprint(
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


def _canonical_series_key(key: PositioningSeriesKey) -> dict[str, str | None]:
    return {
        "source_id": key.source_id,
        "source_series_id": key.source_series_id,
        "instrument_id": key.instrument_id,
        "symbol": key.symbol,
        "asset_class": key.asset_class.value,
        "venue": key.venue,
        "participant_category": key.participant_category.value,
        "measure": key.measure.value,
        "frequency": key.frequency.value,
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
