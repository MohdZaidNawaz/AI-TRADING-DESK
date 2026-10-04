"""Immutable provider-neutral contracts for discrete economic events."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.events import DataDomain, SourceMetadata, SourceTransport
from app.models.schemas import APIModel


class EconomicEventModel(APIModel):
    """Frozen base for economic-event boundary models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EconomicValueUnit(str, Enum):
    PERCENT = "percent"
    PERCENTAGE_POINT = "percentage_point"
    BASIS_POINT = "basis_point"
    INDEX_POINT = "index_point"
    COUNT = "count"
    CURRENCY = "currency"
    RATE = "rate"
    LEVEL = "level"
    UNKNOWN = "unknown"


class EconomicValueScale(str, Enum):
    ONE = "one"
    THOUSAND = "thousand"
    MILLION = "million"
    BILLION = "billion"
    TRILLION = "trillion"


class EconomicReleaseStatus(str, Enum):
    SCHEDULED = "scheduled"
    RELEASED = "released"
    REVISED = "revised"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EconomicImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class EconomicEventSource(EconomicEventModel):
    """Immutable source identity retained across an event lifecycle."""

    source_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    transport: SourceTransport
    domain: DataDomain

    @field_validator("source_id", "display_name", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("source identity values cannot be blank")
            return normalized
        return value

    @model_validator(mode="after")
    def validate_domain(self) -> "EconomicEventSource":
        if self.domain is not DataDomain.ECONOMIC_DATA:
            raise ValueError("economic events must use the economic_data domain")
        return self


class EconomicValue(EconomicEventModel):
    """One finite value whose unit and scale remain explicit and unchanged."""

    value: Decimal
    unit: EconomicValueUnit
    scale: EconomicValueScale = EconomicValueScale.ONE

    @field_validator("value")
    @classmethod
    def validate_finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("economic values must be finite")
        return value


class NormalizedEconomicEvent(EconomicEventModel):
    """Connector-to-ingestion boundary for one economic-event lifecycle version.

    Provider payload dictionaries are intentionally absent. Connectors must map
    their data into this contract before FRIDAY can stage the event.
    """

    source: EconomicEventSource
    occurrence_id: str = Field(min_length=1, max_length=160)
    event_name: str = Field(min_length=1, max_length=240)
    indicator_id: str | None = Field(default=None, max_length=160)
    country: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=120)
    currency: str | None = Field(default=None, max_length=12)
    reference_period: str | None = Field(default=None, max_length=120)
    scheduled_at: datetime
    released_at: datetime | None = None
    release_status: EconomicReleaseStatus
    expected_impact: EconomicImpact = EconomicImpact.UNKNOWN
    previous: EconomicValue | None = None
    revised_previous: EconomicValue | None = None
    forecast: EconomicValue | None = None
    actual: EconomicValue | None = None
    release_version: int = Field(ge=1)
    revision_of_event_id: str | None = Field(default=None, max_length=160)
    received_at: datetime

    @field_validator("source", mode="before")
    @classmethod
    def freeze_source(cls, value: object) -> object:
        if isinstance(value, SourceMetadata):
            return value.model_dump()
        return value

    @field_validator("occurrence_id", "event_name", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("required economic-event text cannot be blank")
            return normalized
        return value

    @field_validator(
        "indicator_id",
        "country",
        "region",
        "currency",
        "reference_period",
        "revision_of_event_id",
        mode="before",
    )
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

    @field_validator("scheduled_at", "released_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("economic-event timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_contract(self) -> "NormalizedEconomicEvent":
        if self.country is None and self.region is None and self.currency is None:
            raise ValueError("at least one of country, region, or currency is required")

        if self.release_status is EconomicReleaseStatus.SCHEDULED:
            if self.released_at is not None or self.actual is not None:
                raise ValueError("scheduled events cannot contain a release time or actual value")
        elif self.release_status in {
            EconomicReleaseStatus.RELEASED,
            EconomicReleaseStatus.REVISED,
        }:
            if self.released_at is None:
                raise ValueError("released and revised events require released_at")
        elif self.release_status in {
            EconomicReleaseStatus.DELAYED,
            EconomicReleaseStatus.CANCELLED,
            EconomicReleaseStatus.UNKNOWN,
        }:
            if self.released_at is not None or self.actual is not None:
                raise ValueError(
                    "delayed, cancelled, and unknown events cannot contain release data"
                )

        if self.release_status is EconomicReleaseStatus.REVISED:
            if self.release_version <= 1:
                raise ValueError("revised events require a release_version greater than one")
            if self.revision_of_event_id is None:
                raise ValueError("revised events require revision_of_event_id")
        elif self.revision_of_event_id is not None:
            raise ValueError("revision_of_event_id is reserved for revised events")
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
            },
            "occurrence_id": self.occurrence_id,
            "event_name": self.event_name,
            "indicator_id": self.indicator_id,
            "country": self.country,
            "region": self.region,
            "currency": self.currency,
            "reference_period": self.reference_period,
            "scheduled_at": self.scheduled_at.isoformat(),
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "release_status": self.release_status.value,
            "expected_impact": self.expected_impact.value,
            "previous": _canonical_value(self.previous),
            "revised_previous": _canonical_value(self.revised_previous),
            "forecast": _canonical_value(self.forecast),
            "actual": _canonical_value(self.actual),
            "release_version": self.release_version,
            "revision_of_event_id": self.revision_of_event_id,
        }
        return _fingerprint(payload)

    @computed_field(return_type=str)
    @property
    def event_id(self) -> str:
        """Stable identity for one immutable lifecycle version."""
        return "economic-event-" + _fingerprint(
            {
                "occurrence_id": self.occurrence_id,
                "release_version": self.release_version,
                "fingerprint": self.fingerprint,
            }
        )


def _canonical_value(value: EconomicValue | None) -> dict[str, str] | None:
    if value is None:
        return None
    normalized = format(value.value.normalize(), "f")
    if Decimal(normalized) == 0:
        normalized = "0"
    return {
        "value": normalized,
        "unit": value.unit.value,
        "scale": value.scale.value,
    }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
