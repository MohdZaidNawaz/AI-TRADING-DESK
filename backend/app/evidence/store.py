"""Bounded, process-local storage for immutable FRIDAY evidence projections."""

from __future__ import annotations

from threading import RLock

from app.evidence.models import (
    EvidenceSnapshot,
    MarketEvidence,
    NewsEvidence,
    canonical_symbol,
    symbols_match,
)
from app.models.events import NormalizedDataEvent, NormalizedNewsResearchEvent


class EvidenceStore:
    """Thread-safe, bounded evidence store with deterministic snapshots.

    The store intentionally has no persistence lifecycle: it belongs to one
    application process and is empty after that process restarts.
    """

    def __init__(
        self,
        *,
        max_market_events: int = 100,
        max_news_events: int = 100,
    ) -> None:
        _validate_limit(max_market_events, "max_market_events")
        _validate_limit(max_news_events, "max_news_events")
        self._max_market_events = max_market_events
        self._max_news_events = max_news_events
        self._market_events: dict[str, MarketEvidence] = {}
        self._news_events: dict[str, NewsEvidence] = {}
        self._lock = RLock()

    def ingest_market_event(self, event: NormalizedDataEvent) -> MarketEvidence:
        """Project and retain one normalized market event."""
        evidence = MarketEvidence.from_event(event)
        with self._lock:
            existing = self._market_events.get(evidence.provenance.event_id)
            if existing is None or _market_sort_key(evidence) < _market_sort_key(existing):
                self._market_events[evidence.provenance.event_id] = evidence
            self._trim(self._market_events, self._max_market_events, _market_sort_key)
            return self._market_events.get(evidence.provenance.event_id, evidence)

    def ingest_news_event(
        self,
        event: NormalizedNewsResearchEvent,
    ) -> NewsEvidence:
        """Project and retain one normalized news/research event."""
        evidence = NewsEvidence.from_event(event)
        with self._lock:
            existing = self._news_events.get(evidence.provenance.event_id)
            if existing is None or _news_sort_key(evidence) < _news_sort_key(existing):
                self._news_events[evidence.provenance.event_id] = evidence
            self._trim(self._news_events, self._max_news_events, _news_sort_key)
            return self._news_events.get(evidence.provenance.event_id, evidence)

    def snapshot(self, *, symbol: str | None = None) -> EvidenceSnapshot:
        """Return a symbol-scoped, immutable, deterministically ordered snapshot."""
        requested_symbol = canonical_symbol(symbol)
        with self._lock:
            market_evidence = tuple(
                sorted(
                    (
                        evidence
                        for evidence in self._market_events.values()
                        if _market_matches_symbol(evidence, requested_symbol)
                    ),
                    key=_market_sort_key,
                )
            )
            news_evidence = tuple(
                sorted(
                    (
                        evidence
                        for evidence in self._news_events.values()
                        if _news_matches_symbol(evidence, requested_symbol)
                    ),
                    key=_news_sort_key,
                )
            )

        return EvidenceSnapshot(
            market_evidence=market_evidence,
            news_evidence=news_evidence,
            symbol=requested_symbol,
        )

    @property
    def market_count(self) -> int:
        with self._lock:
            return len(self._market_events)

    @property
    def news_count(self) -> int:
        with self._lock:
            return len(self._news_events)

    @staticmethod
    def _trim(
        events: dict[str, MarketEvidence] | dict[str, NewsEvidence],
        limit: int,
        sort_key: object,
    ) -> None:
        while len(events) > limit:
            oldest_event_id = max(
                events,
                key=lambda event_id: sort_key(events[event_id]),  # type: ignore[operator]
            )
            del events[oldest_event_id]


def _validate_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _market_matches_symbol(
    evidence: MarketEvidence,
    requested_symbol: str | None,
) -> bool:
    return requested_symbol is None or symbols_match(evidence.symbol, requested_symbol)


def _news_matches_symbol(
    evidence: NewsEvidence,
    requested_symbol: str | None,
) -> bool:
    return requested_symbol is None or any(
        symbols_match(symbol, requested_symbol) for symbol in evidence.symbols
    )


def _market_sort_key(
    evidence: MarketEvidence,
) -> tuple[float, str, str, str, str, str]:
    return (
        -evidence.timestamp.timestamp(),
        evidence.provenance.event_id,
        evidence.provenance.source_id,
        evidence.symbol or "",
        evidence.event,
        repr(evidence),
    )


def _news_sort_key(
    evidence: NewsEvidence,
) -> tuple[float, str, str, str, str, str]:
    return (
        -evidence.published_at.timestamp(),
        evidence.provenance.event_id,
        evidence.provenance.source_id,
        evidence.url,
        evidence.title,
        repr(evidence),
    )
