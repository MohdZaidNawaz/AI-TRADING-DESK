"""Immutable provider-neutral contracts for reported financial-statement evidence.

Fundamental observations are reported accounting facts indexed by a fiscal or
point-in-time reporting period.  They are deliberately separate from prices,
market bars, macroeconomic observations, positioning, estimates, and valuation
calculations.
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


class FundamentalModel(APIModel):
    """Frozen base for provider-neutral financial-statement contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FinancialStatementKind(str, Enum):
    INCOME_STATEMENT = "income_statement"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW_STATEMENT = "cash_flow_statement"


class FundamentalFrequency(str, Enum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    INTERIM = "interim"
    IRREGULAR = "irregular"


class FundamentalReportingScope(str, Enum):
    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"
    UNKNOWN = "unknown"


class FundamentalReferencePeriodKind(str, Enum):
    INTERVAL = "interval"
    POINT_IN_TIME = "point_in_time"


class FundamentalSource(FundamentalModel):
    """Immutable connector identity for one source-scoped issuer dataset."""

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
    def validate_source(self) -> "FundamentalSource":
        if self.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("fundamental sources must use the fundamental_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("fundamental source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class FundamentalReferencePeriod(FundamentalModel):
    """Explicit reporting dates without market-bar timing assumptions."""

    kind: FundamentalReferencePeriodKind
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
    def validate_period(self) -> "FundamentalReferencePeriod":
        if self.end_date < self.start_date:
            raise ValueError("reference-period end_date cannot precede start_date")
        if (
            self.kind is FundamentalReferencePeriodKind.POINT_IN_TIME
            and self.start_date != self.end_date
        ):
            raise ValueError("point-in-time reference periods require matching start and end dates")
        if (
            self.kind is FundamentalReferencePeriodKind.INTERVAL
            and self.end_date <= self.start_date
        ):
            raise ValueError("interval reference periods require a positive duration")
        return self

    @computed_field(return_type=str)
    @property
    def period_id(self) -> str:
        """Stable semantic identity; display labels intentionally do not participate."""
        return _stable_id(
            "fundamental-period",
            self.kind.value,
            self.start_date.isoformat(),
            self.end_date.isoformat(),
        )


class FundamentalSeriesKey(FundamentalModel):
    """Stable source-scoped identity for one reported financial-statement metric."""

    source_id: str = Field(min_length=1, max_length=80)
    source_series_id: str = Field(min_length=1, max_length=160)
    issuer_id: str = Field(min_length=1, max_length=160)
    symbol: str | None = Field(default=None, max_length=40)
    venue: str | None = Field(default=None, max_length=80)
    statement_kind: FinancialStatementKind
    metric_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    frequency: FundamentalFrequency
    reporting_scope: FundamentalReportingScope = FundamentalReportingScope.UNKNOWN
    currency: str | None = Field(default=None, max_length=12)
    value_unit: EconomicValueUnit
    value_scale: EconomicValueScale = EconomicValueScale.ONE

    @field_validator("source_id", "source_series_id", "issuer_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("fundamental-series identity values cannot be blank")
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
    def validate_currency(self) -> "FundamentalSeriesKey":
        if self.value_unit is EconomicValueUnit.CURRENCY and self.currency is None:
            raise ValueError("currency-valued fundamental series require currency")
        return self

    @computed_field(return_type=str)
    @property
    def series_id(self) -> str:
        return _stable_id(
            "fundamental-series",
            self.source_id,
            self.source_series_id,
            self.issuer_id,
            self.symbol or "",
            self.venue or "",
            self.statement_kind.value,
            self.metric_id,
            self.frequency.value,
            self.reporting_scope.value,
            self.currency or "",
            self.value_unit.value,
            self.value_scale.value,
        )


class NormalizedFundamentalObservation(FundamentalModel):
    """One immutable reported accounting value or source-issued revision."""

    source: FundamentalSource
    series_key: FundamentalSeriesKey
    reference_period: FundamentalReferencePeriod
    value: EconomicValue
    observation_version: int = Field(ge=1)
    revision_of_observation_id: str | None = Field(default=None, max_length=160)
    reported_at: datetime | None = None
    filed_at: datetime | None = None
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

    @field_validator("reported_at", "filed_at", "published_at", "vintage_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fundamental-observation timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_observation(self) -> "NormalizedFundamentalObservation":
        if self.source.source_id != self.series_key.source_id:
            raise ValueError("fundamental observation source_id must match the series key")
        if self.value.unit is not self.series_key.value_unit:
            raise ValueError("fundamental observation value unit must match the series key")
        if self.value.scale is not self.series_key.value_scale:
            raise ValueError("fundamental observation value scale must match the series key")
        if self.observation_version == 1 and self.revision_of_observation_id is not None:
            raise ValueError("version-one observations cannot declare a revision link")
        if self.observation_version > 1 and self.revision_of_observation_id is None:
            raise ValueError("revised observations require revision_of_observation_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        """Fingerprint normalized content while excluding local receipt-time noise."""
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
            "observation_version": self.observation_version,
            "revision_of_observation_id": self.revision_of_observation_id,
            "reported_at": _optional_timestamp(self.reported_at),
            "filed_at": _optional_timestamp(self.filed_at),
            "published_at": _optional_timestamp(self.published_at),
            "vintage_at": _optional_timestamp(self.vintage_at),
        }
        return _fingerprint(payload)

    @computed_field(return_type=str)
    @property
    def observation_id(self) -> str:
        return "fundamental-observation-" + _fingerprint(
            {
                "series_id": self.series_key.series_id,
                "reference_period_id": self.reference_period.period_id,
                "observation_version": self.observation_version,
                "fingerprint": self.fingerprint,
            }
        )


def _canonical_series_key(key: FundamentalSeriesKey) -> dict[str, str | None]:
    return {
        "source_id": key.source_id,
        "source_series_id": key.source_series_id,
        "issuer_id": key.issuer_id,
        "symbol": key.symbol,
        "venue": key.venue,
        "statement_kind": key.statement_kind.value,
        "metric_id": key.metric_id,
        "frequency": key.frequency.value,
        "reporting_scope": key.reporting_scope.value,
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
