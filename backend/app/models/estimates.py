"""Immutable provider-neutral contracts for source-reported analyst estimates.

Estimate observations preserve a provider's stated expectation for an issuer
and target reporting period.  They are intentionally separate from reported
financial statements, prices, valuation calculations, recommendations, and
FRIDAY-generated forecasts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
import json
import re

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.schemas import APIModel


class EstimateModel(APIModel):
    """Frozen base for provider-neutral analyst-estimate contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EstimateFrequency(str, Enum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    INTERIM = "interim"
    IRREGULAR = "irregular"


class EstimateReferencePeriodKind(str, Enum):
    INTERVAL = "interval"
    POINT_IN_TIME = "point_in_time"


class EstimateStatistic(str, Enum):
    """Only source-reported numeric estimate statistics supported in Phase 5B-6."""

    CONSENSUS_MEAN = "consensus_mean"
    MEDIAN = "median"
    HIGH = "high"
    LOW = "low"
    ANALYST_COUNT = "analyst_count"


class EstimateSource(EstimateModel):
    """Immutable identity for one source-scoped analyst-estimate dataset."""

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
    def validate_source(self) -> "EstimateSource":
        if self.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("estimate sources must use the fundamental_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("estimate source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class EstimateReferencePeriod(EstimateModel):
    """Explicit target period, deliberately separate from the estimate's ``as_of`` time."""

    kind: EstimateReferencePeriodKind
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
    def validate_period(self) -> "EstimateReferencePeriod":
        if self.end_date < self.start_date:
            raise ValueError("estimate reference-period end_date cannot precede start_date")
        if (
            self.kind is EstimateReferencePeriodKind.POINT_IN_TIME
            and self.start_date != self.end_date
        ):
            raise ValueError("point-in-time estimate periods require matching start and end dates")
        if self.kind is EstimateReferencePeriodKind.INTERVAL and self.end_date <= self.start_date:
            raise ValueError("interval estimate periods require a positive duration")
        return self

    @computed_field(return_type=str)
    @property
    def period_id(self) -> str:
        """Stable semantic identity; labels are never identity shortcuts."""
        return _stable_id(
            "estimate-period",
            self.kind.value,
            self.start_date.isoformat(),
            self.end_date.isoformat(),
        )


class EstimateSeriesKey(EstimateModel):
    """Stable source-scoped identity for one source-reported estimate measure."""

    source_id: str = Field(min_length=1, max_length=80)
    source_series_id: str = Field(min_length=1, max_length=160)
    issuer_id: str = Field(min_length=1, max_length=160)
    symbol: str | None = Field(default=None, max_length=40)
    venue: str | None = Field(default=None, max_length=80)
    metric_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    statistic: EstimateStatistic
    frequency: EstimateFrequency
    currency: str | None = Field(default=None, max_length=12)
    value_unit: EconomicValueUnit
    value_scale: EconomicValueScale = EconomicValueScale.ONE

    @field_validator("source_id", "source_series_id", "issuer_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("estimate-series identity values cannot be blank")
            return normalized
        return value

    @field_validator("metric_id", mode="before")
    @classmethod
    def normalize_metric_id(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                raise ValueError("metric_id cannot be blank")
            return normalized
        return value

    @field_validator("symbol", "venue", "currency", mode="before")
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

    @model_validator(mode="after")
    def validate_currency(self) -> "EstimateSeriesKey":
        if self.value_unit is EconomicValueUnit.CURRENCY and self.currency is None:
            raise ValueError("currency-valued estimate series require currency")
        if self.statistic is EstimateStatistic.ANALYST_COUNT and self.value_unit is not EconomicValueUnit.COUNT:
            raise ValueError("analyst-count estimate series require count value_unit")
        return self

    @computed_field(return_type=str)
    @property
    def series_id(self) -> str:
        return _stable_id(
            "estimate-series",
            self.source_id,
            self.source_series_id,
            self.issuer_id,
            self.symbol or "",
            self.venue or "",
            self.metric_id,
            self.statistic.value,
            self.frequency.value,
            self.currency or "",
            self.value_unit.value,
            self.value_scale.value,
        )


class NormalizedEstimateObservation(EstimateModel):
    """One immutable source-reported estimate value or source-issued revision."""

    source: EstimateSource
    series_key: EstimateSeriesKey
    reference_period: EstimateReferencePeriod
    value: EconomicValue
    as_of: datetime
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
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

    @field_validator("as_of", "published_at", "vintage_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("estimate-observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_observation(self) -> "NormalizedEstimateObservation":
        if self.source.source_id != self.series_key.source_id:
            raise ValueError("estimate observation source_id must match the series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("estimate observation value unit must match the series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("estimate observation value scale must match the series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one estimate observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised estimate observations require revision_of_observation_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        """Fingerprint semantic content while excluding local receipt-time noise."""
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
            },
            "value": _canonical_value(self.value),
            "as_of": self.as_of.isoformat(),
            "observation_version": self.observation_version,
            "revision_of_observation_id": self.revision_of_observation_id,
            "published_at": _optional_timestamp(self.published_at),
            "vintage_at": _optional_timestamp(self.vintage_at),
        }
        return _fingerprint(payload)

    @computed_field(return_type=str)
    @property
    def observation_id(self) -> str:
        return "estimate-observation-" + _fingerprint(
            {
                "series_id": self.series_key.series_id,
                "reference_period_id": self.reference_period.period_id,
                "observation_version": self.observation_version,
                "fingerprint": self.fingerprint,
            }
        )


def _canonical_series_key(key: EstimateSeriesKey) -> dict[str, str | None]:
    return {
        "source_id": key.source_id,
        "source_series_id": key.source_series_id,
        "issuer_id": key.issuer_id,
        "symbol": key.symbol,
        "venue": key.venue,
        "metric_id": key.metric_id,
        "statistic": key.statistic.value,
        "frequency": key.frequency.value,
        "currency": key.currency,
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
