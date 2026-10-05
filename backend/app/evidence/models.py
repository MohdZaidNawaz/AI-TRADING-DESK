"""Provider-neutral, immutable evidence projections for FRIDAY specialists.

This module deliberately exposes only fields that are safe and useful to the
agent layer.  Connector payloads, provider-only attributes, and article bodies
remain at their ingestion boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
import json
import math
import re

from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    NormalizedNewsResearchEvent,
    SourceMetadata,
    SourceTransport,
)
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
    NormalizedEconomicEvent,
)
from app.models.market_series import (
    MarketBarState,
    MarketSeriesKey,
    NormalizedMarketBar,
    VolumeKind,
)
from app.models.macro_series import (
    MacroReferencePeriod,
    MacroSeriesKey,
    NormalizedMacroObservation,
)


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    """Stable source and receipt metadata retained with projected evidence."""

    event_id: str
    source_id: str
    source_name: str
    transport: SourceTransport
    domain: DataDomain
    event_timestamp: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class EconomicEventEvidence:
    """Safe immutable projection of one economic-event lifecycle version."""

    occurrence_id: str
    event_id: str
    event_name: str
    indicator_id: str | None
    country: str | None
    region: str | None
    currency: str | None
    reference_period: str | None
    scheduled_at: datetime
    released_at: datetime | None
    release_status: EconomicReleaseStatus
    expected_impact: EconomicImpact
    previous: EconomicValue | None
    revised_previous: EconomicValue | None
    forecast: EconomicValue | None
    actual: EconomicValue | None
    release_version: int
    revision_of_event_id: str | None
    provenance: EvidenceProvenance
    received_at: datetime
    fingerprint: str

    @classmethod
    def from_event(cls, event: NormalizedEconomicEvent) -> "EconomicEventEvidence":
        if not isinstance(event, NormalizedEconomicEvent):
            raise TypeError("event must be a NormalizedEconomicEvent")
        event_timestamp = event.released_at or event.scheduled_at
        return cls(
            occurrence_id=event.occurrence_id,
            event_id=event.event_id,
            event_name=event.event_name,
            indicator_id=event.indicator_id,
            country=event.country,
            region=event.region,
            currency=event.currency,
            reference_period=event.reference_period,
            scheduled_at=_as_utc(event.scheduled_at),
            released_at=(
                _as_utc(event.released_at) if event.released_at is not None else None
            ),
            release_status=event.release_status,
            expected_impact=event.expected_impact,
            previous=event.previous,
            revised_previous=event.revised_previous,
            forecast=event.forecast,
            actual=event.actual,
            release_version=event.release_version,
            revision_of_event_id=event.revision_of_event_id,
            provenance=_provenance_from_source(
                event_id=event.event_id,
                source=event.source,
                event_timestamp=event_timestamp,
                received_at=event.received_at,
            ),
            received_at=_as_utc(event.received_at),
            fingerprint=event.fingerprint,
        )


@dataclass(frozen=True, slots=True)
class MacroObservationEvidence:
    """Safe immutable projection of one macro observation or retained vintage."""

    observation_id: str
    series_id: str
    reference_period: MacroReferencePeriod
    value: EconomicValue
    observation_version: int
    revision_of_observation_id: str | None
    observed_at: datetime | None
    effective_at: datetime | None
    published_at: datetime | None
    vintage_at: datetime | None
    received_at: datetime
    provenance: EvidenceProvenance
    fingerprint: str

    @classmethod
    def from_observation(
        cls,
        observation: NormalizedMacroObservation,
    ) -> "MacroObservationEvidence":
        if not isinstance(observation, NormalizedMacroObservation):
            raise TypeError("observation must be a NormalizedMacroObservation")
        reference_time = (
            observation.published_at
            or observation.observed_at
            or observation.effective_at
            or observation.received_at
        )
        return cls(
            observation_id=observation.observation_id,
            series_id=observation.series_key.series_id,
            reference_period=observation.reference_period,
            value=observation.value,
            observation_version=observation.observation_version,
            revision_of_observation_id=observation.revision_of_observation_id,
            observed_at=(
                _as_utc(observation.observed_at)
                if observation.observed_at is not None
                else None
            ),
            effective_at=(
                _as_utc(observation.effective_at)
                if observation.effective_at is not None
                else None
            ),
            published_at=(
                _as_utc(observation.published_at)
                if observation.published_at is not None
                else None
            ),
            vintage_at=(
                _as_utc(observation.vintage_at)
                if observation.vintage_at is not None
                else None
            ),
            received_at=_as_utc(observation.received_at),
            provenance=_provenance_from_source(
                event_id=observation.observation_id,
                source=observation.source,
                event_timestamp=reference_time,
                received_at=observation.received_at,
            ),
            fingerprint=observation.fingerprint,
        )


@dataclass(frozen=True, slots=True)
class MacroSeriesProvenance:
    """Source and receipt metadata for a bounded macro-series snapshot."""

    source_id: str
    source_name: str
    transport: SourceTransport
    domain: DataDomain
    origin_id: str | None
    origin_name: str | None
    first_received_at: datetime
    latest_received_at: datetime


@dataclass(frozen=True, slots=True)
class MacroSeriesEvidence:
    """Deterministic provider-neutral evidence for one persistent macro series."""

    series_id: str
    key: MacroSeriesKey
    observations: tuple[MacroObservationEvidence, ...]
    current_observations: tuple[MacroObservationEvidence, ...]
    latest_reference_period_end: date
    latest_observed_at: datetime | None
    latest_effective_at: datetime | None
    latest_published_at: datetime | None
    latest_vintage_at: datetime | None
    provenance: MacroSeriesProvenance
    fingerprint: str

    @classmethod
    def from_observations(
        cls,
        key: MacroSeriesKey,
        observations: tuple[NormalizedMacroObservation, ...],
    ) -> "MacroSeriesEvidence":
        if not observations:
            raise ValueError("macro-series evidence requires at least one observation")
        ordered_source = tuple(sorted(observations, key=_macro_observation_sort_key))
        if any(item.series_key != key for item in ordered_source):
            raise ValueError("all observations must belong to the supplied macro series key")
        first = ordered_source[0]
        if any(item.source != first.source for item in ordered_source):
            raise ValueError("a macro series cannot mix source metadata")

        projected = tuple(MacroObservationEvidence.from_observation(item) for item in ordered_source)
        current_by_period: dict[str, MacroObservationEvidence] = {}
        for item in projected:
            period_id = item.reference_period.period_id
            existing = current_by_period.get(period_id)
            if existing is None or _macro_current_rank(item) > _macro_current_rank(existing):
                current_by_period[period_id] = item
        current = tuple(sorted(current_by_period.values(), key=_macro_evidence_sort_key))
        latest = max(current, key=_macro_evidence_sort_key)
        provenance = MacroSeriesProvenance(
            source_id=first.source.source_id,
            source_name=first.source.display_name,
            transport=first.source.transport,
            domain=first.source.domain,
            origin_id=first.source.origin_id,
            origin_name=first.source.origin_name,
            first_received_at=min(item.received_at for item in projected),
            latest_received_at=max(item.received_at for item in projected),
        )
        return cls(
            series_id=key.series_id,
            key=key,
            observations=projected,
            current_observations=current,
            latest_reference_period_end=latest.reference_period.end_date,
            latest_observed_at=latest.observed_at,
            latest_effective_at=latest.effective_at,
            latest_published_at=latest.published_at,
            latest_vintage_at=latest.vintage_at,
            provenance=provenance,
            fingerprint=_macro_series_fingerprint(key, projected),
        )


@dataclass(frozen=True, slots=True)
class MacroSeriesSnapshot:
    """Bounded immutable output from the dedicated macro-series store."""

    series: tuple[MacroSeriesEvidence, ...] = ()
    fingerprint: str = sha256(b"[]").hexdigest()


@dataclass(frozen=True, slots=True)
class OverallSentimentEvidence:
    """Provider-neutral overall sentiment attached to a news item."""

    score: float
    label: str


@dataclass(frozen=True, slots=True)
class TopicRelevanceEvidence:
    """Provider-neutral relevance attached to a news topic."""

    topic: str
    relevance_score: float


@dataclass(frozen=True, slots=True)
class SymbolSentimentEvidence:
    """Provider-neutral relevance and sentiment attached to a symbol."""

    symbol: str
    relevance_score: float
    sentiment_score: float
    sentiment_label: str


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    """Safe, immutable projection of one normalized market-data event."""

    provenance: EvidenceProvenance
    event_type: EventType
    event: str
    asset_class: AssetClass
    symbol: str | None
    exchange: str | None
    timeframe: str | None
    alert_name: str | None
    message: str | None
    price: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    indicator_values: tuple[tuple[str, float], ...]
    timestamp: datetime

    @classmethod
    def from_event(cls, event: NormalizedDataEvent) -> "MarketEvidence":
        """Project an already-normalized market event without raw payload data."""
        if not isinstance(event, NormalizedDataEvent):
            raise TypeError("event must be a NormalizedDataEvent")

        return cls(
            provenance=_provenance_from_source(
                event_id=event.event_id,
                source=event.source,
                event_timestamp=event.timestamp,
                received_at=event.received_at,
            ),
            event_type=event.event_type,
            event=event.event,
            asset_class=event.asset_class,
            symbol=event.symbol,
            exchange=event.exchange,
            timeframe=event.timeframe,
            alert_name=event.alert_name,
            message=event.message,
            price=_finite_number(event.price),
            open=_finite_number(event.open),
            high=_finite_number(event.high),
            low=_finite_number(event.low),
            close=_finite_number(event.close),
            volume=_finite_number(event.volume),
            indicator_values=_finite_indicator_values(event.indicator_values),
            timestamp=_as_utc(event.timestamp),
        )


@dataclass(frozen=True, slots=True)
class MarketBarEvidence:
    """Immutable safe projection of one normalized historical market bar."""

    bar_id: str
    opened_at: datetime
    closed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    volume_kind: VolumeKind
    state: MarketBarState
    received_at: datetime
    revision: int | None

    @classmethod
    def from_bar(cls, bar: NormalizedMarketBar) -> "MarketBarEvidence":
        if not isinstance(bar, NormalizedMarketBar):
            raise TypeError("bar must be a NormalizedMarketBar")
        return cls(
            bar_id=bar.bar_id,
            opened_at=_as_utc(bar.opened_at),
            closed_at=_as_utc(bar.closed_at),
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume) if bar.volume is not None else None,
            volume_kind=bar.volume_kind,
            state=bar.state,
            received_at=_as_utc(bar.received_at),
            revision=bar.revision,
        )


@dataclass(frozen=True, slots=True)
class SeriesGap:
    """Observed discontinuity; missing intervals are never invented or filled."""

    expected_open_at: datetime
    actual_open_at: datetime
    missing_interval_count: int


@dataclass(frozen=True, slots=True)
class SeriesContinuity:
    expected_interval_seconds: int
    first_opened_at: datetime
    last_closed_at: datetime
    is_ordered: bool
    is_contiguous: bool
    missing_interval_count: int
    gaps: tuple[SeriesGap, ...]


@dataclass(frozen=True, slots=True)
class MarketSeriesProvenance:
    source_id: str
    source_name: str
    transport: SourceTransport
    domain: DataDomain
    first_received_at: datetime
    latest_received_at: datetime


@dataclass(frozen=True, slots=True)
class MarketSeriesEvidence:
    """Deterministic provider-neutral historical-series evidence."""

    series_id: str
    key: MarketSeriesKey
    symbol: str
    asset_class: AssetClass
    venue: str | None
    timeframe: str
    bars: tuple[MarketBarEvidence, ...]
    oldest_completed_at: datetime | None
    latest_completed_at: datetime | None
    completed_count: int
    provisional_count: int
    continuity: SeriesContinuity
    provenance: MarketSeriesProvenance
    fingerprint: str

    @classmethod
    def from_bars(
        cls,
        key: MarketSeriesKey,
        bars: tuple[NormalizedMarketBar, ...],
    ) -> "MarketSeriesEvidence":
        if not bars:
            raise ValueError("market-series evidence requires at least one bar")
        ordered = tuple(sorted(bars, key=lambda item: (item.opened_at, item.bar_id)))
        if any(bar.series_key != key for bar in ordered):
            raise ValueError("all bars must belong to the supplied series key")

        first = ordered[0]
        if any(bar.asset_class is not first.asset_class for bar in ordered):
            raise ValueError("a market series cannot mix asset classes")
        if any(bar.source != first.source for bar in ordered):
            raise ValueError("a market series cannot mix source metadata")

        projected = tuple(MarketBarEvidence.from_bar(bar) for bar in ordered)
        completed = tuple(
            bar for bar in projected if bar.state is MarketBarState.COMPLETED
        )
        continuity = _series_continuity(projected, first.timeframe_seconds)
        provenance = MarketSeriesProvenance(
            source_id=first.source.source_id,
            source_name=first.source.display_name,
            transport=first.source.transport,
            domain=first.source.domain,
            first_received_at=min(bar.received_at for bar in projected),
            latest_received_at=max(bar.received_at for bar in projected),
        )
        fingerprint = _series_fingerprint(key, projected)
        return cls(
            series_id=key.series_id,
            key=key,
            symbol=first.symbol,
            asset_class=first.asset_class,
            venue=first.venue,
            timeframe=first.timeframe,
            bars=projected,
            oldest_completed_at=min(
                (bar.closed_at for bar in completed),
                default=None,
            ),
            latest_completed_at=max(
                (bar.closed_at for bar in completed),
                default=None,
            ),
            completed_count=len(completed),
            provisional_count=len(projected) - len(completed),
            continuity=continuity,
            provenance=provenance,
            fingerprint=fingerprint,
        )

    @property
    def bar_count(self) -> int:
        return len(self.bars)

    @property
    def latest_bar(self) -> MarketBarEvidence:
        return self.bars[-1]

    @property
    def latest_completed_bar(self) -> MarketBarEvidence | None:
        return next(
            (bar for bar in reversed(self.bars) if bar.state is MarketBarState.COMPLETED),
            None,
        )


@dataclass(frozen=True, slots=True)
class MarketSeriesSnapshot:
    """Bounded immutable output from the dedicated historical-series store."""

    series: tuple[MarketSeriesEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class EconomicEventSnapshot:
    """Bounded immutable output from the economic-event store."""

    events: tuple[EconomicEventEvidence, ...] = ()
    fingerprint: str = sha256(b"[]").hexdigest()


@dataclass(frozen=True, slots=True)
class NewsEvidence:
    """Safe, immutable projection of one normalized news/research event."""

    provenance: EvidenceProvenance
    provider: str
    title: str
    url: str
    published_at: datetime
    symbols: tuple[str, ...]
    topics: tuple[str, ...]
    authors: tuple[str, ...]
    publisher_domain: str | None
    category: str | None
    image_url: str | None
    overall_sentiment: OverallSentimentEvidence | None
    topic_relevance: tuple[TopicRelevanceEvidence, ...]
    symbol_sentiment: tuple[SymbolSentimentEvidence, ...]
    summary: str | None

    @classmethod
    def from_event(cls, event: NormalizedNewsResearchEvent) -> "NewsEvidence":
        """Project normalized research without exposing article content or raw data."""
        if not isinstance(event, NormalizedNewsResearchEvent):
            raise TypeError("event must be a NormalizedNewsResearchEvent")

        overall_sentiment = (
            OverallSentimentEvidence(
                score=float(event.overall_sentiment.score),
                label=event.overall_sentiment.label,
            )
            if event.overall_sentiment is not None
            else None
        )
        return cls(
            provenance=_provenance_from_source(
                event_id=event.event_id,
                source=event.source,
                event_timestamp=event.timestamp,
                received_at=event.received_at,
            ),
            provider=event.provider,
            title=event.title,
            url=str(event.url),
            published_at=_as_utc(event.timestamp),
            symbols=tuple(event.symbols),
            topics=tuple(event.topics),
            authors=tuple(event.authors),
            publisher_domain=event.publisher_domain,
            category=event.category,
            image_url=str(event.image_url) if event.image_url is not None else None,
            overall_sentiment=overall_sentiment,
            topic_relevance=tuple(
                TopicRelevanceEvidence(
                    topic=item.topic,
                    relevance_score=float(item.relevance_score),
                )
                for item in event.topic_relevance
            ),
            symbol_sentiment=tuple(
                SymbolSentimentEvidence(
                    symbol=item.symbol,
                    relevance_score=float(item.relevance_score),
                    sentiment_score=float(item.sentiment_score),
                    sentiment_label=item.sentiment_label,
                )
                for item in event.symbol_sentiment
            ),
            summary=event.summary,
        )


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """A deterministic, immutable view of evidence available to an analysis run."""

    market_evidence: tuple[MarketEvidence, ...] = ()
    news_evidence: tuple[NewsEvidence, ...] = ()
    market_series: tuple[MarketSeriesEvidence, ...] = ()
    economic_events: tuple[EconomicEventEvidence, ...] = ()
    macro_series: tuple[MacroSeriesEvidence, ...] = ()
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvidence:
    """The provider-neutral evidence subset assigned to one specialist."""

    market_evidence: tuple[MarketEvidence, ...] = ()
    news_evidence: tuple[NewsEvidence, ...] = ()
    market_series: tuple[MarketSeriesEvidence, ...] = ()
    economic_events: tuple[EconomicEventEvidence, ...] = ()
    macro_series: tuple[MacroSeriesEvidence, ...] = ()
    provenance: tuple[EvidenceProvenance, ...] = ()


def canonical_symbol(symbol: str | None) -> str | None:
    """Return a small generic canonical symbol representation for matching.

    This intentionally handles only obvious presentation differences, such as
    ``XAUUSD`` and ``XAU/USD``.  A provider namespace before a colon is removed
    because source exchange/venue is retained separately in evidence metadata.
    """
    if symbol is None:
        return None
    normalized = symbol.strip().upper()
    if not normalized:
        return None
    if ":" in normalized:
        normalized = normalized.rsplit(":", maxsplit=1)[-1]
    return re.sub(r"[^A-Z0-9]", "", normalized) or None


def symbols_match(left: str | None, right: str | None) -> bool:
    """Compare obvious equivalent symbol spellings without an instrument master."""
    left_canonical = canonical_symbol(left)
    right_canonical = canonical_symbol(right)
    return left_canonical is not None and left_canonical == right_canonical


def _provenance_from_source(
    *,
    event_id: str,
    source: SourceMetadata | EconomicEventSource,
    event_timestamp: datetime,
    received_at: datetime,
) -> EvidenceProvenance:
    return EvidenceProvenance(
        event_id=event_id,
        source_id=source.source_id,
        source_name=source.display_name,
        transport=source.transport,
        domain=source.domain,
        event_timestamp=_as_utc(event_timestamp),
        received_at=_as_utc(received_at),
    )


def _as_utc(value: datetime) -> datetime:
    """Preserve instant semantics while making projection timestamps comparable."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence timestamps must include a timezone")
    return value.astimezone(UTC)


def _finite_number(value: float | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _finite_indicator_values(
    values: dict[str, float],
) -> tuple[tuple[str, float], ...]:
    projected: list[tuple[str, float]] = []
    for key, value in sorted(values.items()):
        normalized = _finite_number(value)
        if normalized is not None:
            projected.append((key, normalized))
    return tuple(projected)


def _series_continuity(
    bars: tuple[MarketBarEvidence, ...],
    expected_seconds: int,
) -> SeriesContinuity:
    gaps: list[SeriesGap] = []
    missing_count = 0
    for previous, current in zip(bars, bars[1:], strict=False):
        expected_open = previous.opened_at + (previous.closed_at - previous.opened_at)
        if current.opened_at == expected_open:
            continue
        delta_seconds = (current.opened_at - previous.opened_at).total_seconds()
        missing = max(0, math.ceil(delta_seconds / expected_seconds) - 1)
        missing_count += missing
        gaps.append(
            SeriesGap(
                expected_open_at=expected_open,
                actual_open_at=current.opened_at,
                missing_interval_count=missing,
            )
        )
    return SeriesContinuity(
        expected_interval_seconds=expected_seconds,
        first_opened_at=bars[0].opened_at,
        last_closed_at=bars[-1].closed_at,
        is_ordered=all(
            previous.opened_at < current.opened_at
            for previous, current in zip(bars, bars[1:], strict=False)
        ),
        is_contiguous=not gaps,
        missing_interval_count=missing_count,
        gaps=tuple(gaps),
    )


def _series_fingerprint(
    key: MarketSeriesKey,
    bars: tuple[MarketBarEvidence, ...],
) -> str:
    payload = {
        "series_id": key.series_id,
        "bars": [
            {
                "bar_id": bar.bar_id,
                "opened_at": bar.opened_at.isoformat(),
                "closed_at": bar.closed_at.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "volume_kind": bar.volume_kind.value,
                "state": bar.state.value,
                "received_at": bar.received_at.isoformat(),
                "revision": bar.revision,
            }
            for bar in bars
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _macro_observation_sort_key(
    observation: NormalizedMacroObservation,
) -> tuple[date, date, str, int, str]:
    return (
        observation.reference_period.start_date,
        observation.reference_period.end_date,
        observation.reference_period.period_id,
        observation.observation_version,
        observation.observation_id,
    )


def _macro_evidence_sort_key(
    evidence: MacroObservationEvidence,
) -> tuple[date, date, str, int, str]:
    return (
        evidence.reference_period.start_date,
        evidence.reference_period.end_date,
        evidence.reference_period.period_id,
        evidence.observation_version,
        evidence.observation_id,
    )


def _macro_current_rank(
    evidence: MacroObservationEvidence,
) -> tuple[int, datetime, str]:
    return (
        evidence.observation_version,
        evidence.vintage_at or evidence.published_at or evidence.received_at,
        evidence.observation_id,
    )


def _macro_series_fingerprint(
    key: MacroSeriesKey,
    observations: tuple[MacroObservationEvidence, ...],
) -> str:
    payload = {
        "series_id": key.series_id,
        "observations": [
            {
                "observation_id": item.observation_id,
                "fingerprint": item.fingerprint,
                "reference_period_id": item.reference_period.period_id,
                "observation_version": item.observation_version,
            }
            for item in observations
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
