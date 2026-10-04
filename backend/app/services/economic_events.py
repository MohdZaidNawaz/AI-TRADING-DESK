"""Internal provider-neutral ingestion boundary for economic events."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import EconomicEventConsumer
from app.models.economic_events import NormalizedEconomicEvent
from app.services.friday_core import get_friday_core


class EconomicEventIngestionService:
    """Stage validated lifecycle versions without fetching or interpreting data."""

    def __init__(self, *, consumer: EconomicEventConsumer) -> None:
        self._consumer = consumer

    def ingest(self, event: NormalizedEconomicEvent) -> object:
        if not isinstance(event, NormalizedEconomicEvent):
            raise TypeError("event must be a NormalizedEconomicEvent")
        return self._consumer.ingest_economic_event(event)

    def ingest_many(
        self,
        events: tuple[NormalizedEconomicEvent, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(event) for event in events)


@lru_cache
def get_economic_event_ingestion_service() -> EconomicEventIngestionService:
    """Return the process-local boundary; no public API route uses it."""
    return EconomicEventIngestionService(consumer=get_friday_core())
