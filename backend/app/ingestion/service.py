"""Shared hand-off from validated source events to FRIDAY Core."""

from app.ingestion.contracts import EventConsumer
from app.models.events import NormalizedDataEvent


class IngestionService:
    """Provider-agnostic ingestion boundary used by API transports and future feeds."""

    def __init__(self, consumer: EventConsumer) -> None:
        self._consumer = consumer

    def ingest(self, event: NormalizedDataEvent) -> object:
        return self._consumer.ingest_event(event)
