"""Provider-neutral, immutable evidence projections for FRIDAY specialists.

This module deliberately exposes only fields that are safe and useful to the
agent layer.  Connector payloads, provider-only attributes, and article bodies
remain at their ingestion boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvidence:
    """The provider-neutral evidence subset assigned to one specialist."""

    market_evidence: tuple[MarketEvidence, ...] = ()
    news_evidence: tuple[NewsEvidence, ...] = ()
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
    source: SourceMetadata,
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
