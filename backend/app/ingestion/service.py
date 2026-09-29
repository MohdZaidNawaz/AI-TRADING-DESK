"""Shared hand-off from validated source events to FRIDAY Core."""

from collections import deque
from threading import Lock

from app.ingestion.contracts import (
    EventConsumer,
    MarketDataClient,
    NewsEvidenceConsumer,
    SourceConnector,
)
from app.models.events import NormalizedDataEvent, NormalizedNewsResearchEvent


class IngestionService:
    """Provider-agnostic ingestion boundary used by API transports and future feeds."""

    def __init__(self, consumer: EventConsumer) -> None:
        self._consumer = consumer

    def ingest(self, event: NormalizedDataEvent) -> object:
        return self._consumer.ingest_event(event)


class MarketSnapshotService:
    """Provider-neutral fetch, normalize, and ingest workflow."""

    def __init__(
        self,
        *,
        client: MarketDataClient,
        connector: SourceConnector,
        ingestion: IngestionService,
    ) -> None:
        self._client = client
        self._connector = connector
        self._ingestion = ingestion

    async def snapshot(self, symbol: str) -> NormalizedDataEvent:
        payload = await self._client.fetch_snapshot(symbol)
        event = self._connector.normalize(payload)
        self._ingestion.ingest(event)
        return event


class NewsResearchIngestionService:
    """Bounded staging boundary for normalized news/research events."""

    def __init__(
        self,
        *,
        max_events: int = 100,
        consumer: NewsEvidenceConsumer | None = None,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._recent_events: deque[NormalizedNewsResearchEvent] = deque(
            maxlen=max_events
        )
        self._event_lock = Lock()
        self._consumer = consumer

    def ingest(
        self,
        event: NormalizedNewsResearchEvent,
    ) -> NormalizedNewsResearchEvent:
        if not isinstance(event, NormalizedNewsResearchEvent):
            raise TypeError("event must be a NormalizedNewsResearchEvent")
        with self._event_lock:
            self._recent_events.append(event)
        if self._consumer is not None:
            self._consumer.ingest_news_event(event)
        return event

    @property
    def recent_events(self) -> tuple[NormalizedNewsResearchEvent, ...]:
        with self._event_lock:
            return tuple(self._recent_events)
