"""Provider-neutral contracts for validated historical market bars."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import re

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator, model_validator

from app.models.events import AssetClass, DataDomain, SourceMetadata, SourceTransport
from app.models.schemas import APIModel


_TIMEFRAME_PATTERN = re.compile(r"^([1-9][0-9]*)(s|m|h|d|w)$", re.IGNORECASE)
_TIMEFRAME_SECONDS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}


class MarketSeriesModel(APIModel):
    """Frozen base for historical-series contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class VolumeKind(str, Enum):
    UNKNOWN = "unknown"
    REAL = "real"
    EXCHANGE = "exchange"
    TICK = "tick"
    UNAVAILABLE = "unavailable"


class MarketBarState(str, Enum):
    COMPLETED = "completed"
    PROVISIONAL = "provisional"


class MarketSeriesSource(MarketSeriesModel):
    """Immutable source identity retained with historical bars."""

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
    def validate_market_domain(self) -> "MarketSeriesSource":
        if self.domain is not DataDomain.MARKET_DATA:
            raise ValueError("historical market bars must use the market_data domain")
        return self


class MarketSeriesKey(MarketSeriesModel):
    """Stable provider-neutral identity for one market series."""

    source_id: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=40)
    venue: str | None = Field(default=None, max_length=80)
    timeframe: str = Field(min_length=2, max_length=20)

    @field_validator("source_id", mode="before")
    @classmethod
    def normalize_source_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return canonical_market_symbol(value) if isinstance(value, str) else value

    @field_validator("venue", mode="before")
    @classmethod
    def normalize_venue(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            return normalized or None
        return value

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_timeframe(cls, value: object) -> object:
        return canonical_timeframe(value) if isinstance(value, str) else value

    @computed_field(return_type=str)
    @property
    def series_id(self) -> str:
        return _stable_id(
            "market-series",
            self.source_id,
            self.symbol,
            self.venue or "",
            self.timeframe,
        )


class NormalizedMarketBar(MarketSeriesModel):
    """One strictly validated, provider-neutral OHLCV bar.

    ``bar_id`` is semantic: provider revisions retain the same identity and are
    resolved by the bounded series store instead of appearing as duplicate bars.
    """

    source: MarketSeriesSource
    symbol: str = Field(min_length=1, max_length=40)
    asset_class: AssetClass = AssetClass.UNKNOWN
    venue: str | None = Field(default=None, max_length=80)
    timeframe: str = Field(min_length=2, max_length=20)
    opened_at: datetime
    closed_at: datetime
    open: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    close: FiniteFloat
    volume: FiniteFloat | None = None
    volume_kind: VolumeKind = VolumeKind.UNKNOWN
    state: MarketBarState
    received_at: datetime
    revision: int | None = Field(default=None, ge=0)

    @field_validator("source", mode="before")
    @classmethod
    def freeze_source(cls, value: object) -> object:
        if isinstance(value, SourceMetadata):
            return value.model_dump()
        return value

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return canonical_market_symbol(value) if isinstance(value, str) else value

    @field_validator("venue", mode="before")
    @classmethod
    def normalize_venue(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            return normalized or None
        return value

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_timeframe(cls, value: object) -> object:
        return canonical_timeframe(value) if isinstance(value, str) else value

    @field_validator("opened_at", "closed_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market-bar timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_bar(self) -> "NormalizedMarketBar":
        expected_seconds = timeframe_seconds(self.timeframe)
        if self.closed_at <= self.opened_at:
            raise ValueError("closed_at must be later than opened_at")
        if (self.closed_at - self.opened_at).total_seconds() != expected_seconds:
            raise ValueError("bar duration must match the normalized timeframe")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be greater than or equal to open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be less than or equal to open, close, and high")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.volume is None and self.volume_kind not in {
            VolumeKind.UNKNOWN,
            VolumeKind.UNAVAILABLE,
        }:
            raise ValueError("a declared volume kind requires a volume value")
        if self.volume is not None and self.volume_kind is VolumeKind.UNAVAILABLE:
            raise ValueError("unavailable volume cannot include a volume value")
        if self.state is MarketBarState.COMPLETED and self.received_at < self.closed_at:
            raise ValueError("a completed bar cannot be received before its close")
        return self

    @computed_field(return_type=int)
    @property
    def timeframe_seconds(self) -> int:
        return timeframe_seconds(self.timeframe)

    @computed_field(return_type=str)
    @property
    def bar_id(self) -> str:
        return _stable_id(
            "market-bar",
            self.source.source_id,
            self.symbol,
            self.venue or "",
            self.timeframe,
            self.opened_at.isoformat(),
        )

    @property
    def series_key(self) -> MarketSeriesKey:
        return MarketSeriesKey(
            source_id=self.source.source_id,
            symbol=self.symbol,
            venue=self.venue,
            timeframe=self.timeframe,
        )

    def content_fingerprint(self) -> str:
        """Fingerprint all normalized content for deterministic revision ties."""
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


def canonical_market_symbol(value: str) -> str:
    """Canonicalize presentation differences without provider-specific aliases."""
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("symbol cannot be blank")
    if ":" in normalized:
        normalized = normalized.rsplit(":", maxsplit=1)[-1]
    canonical = re.sub(r"[^A-Z0-9]", "", normalized)
    if not canonical:
        raise ValueError("symbol must contain letters or numbers")
    return canonical


def canonical_timeframe(value: str) -> str:
    normalized = value.strip().lower()
    match = _TIMEFRAME_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError("timeframe must be a positive value using s, m, h, d, or w")
    return f"{int(match.group(1))}{match.group(2).lower()}"


def timeframe_seconds(value: str) -> int:
    normalized = canonical_timeframe(value)
    match = _TIMEFRAME_PATTERN.fullmatch(normalized)
    if match is None:  # pragma: no cover - canonical_timeframe already guards this
        raise ValueError("invalid timeframe")
    return int(match.group(1)) * _TIMEFRAME_SECONDS[match.group(2).lower()]


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    digest = sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"
