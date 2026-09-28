"""Shared hand-off from validated source events to FRIDAY Core."""

from app.ingestion.contracts import EventConsumer, MarketDataClient, SourceConnector
from app.models.events import NormalizedDataEvent


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
