"""Internal provider-neutral ingestion boundary for positioning observations."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import PositioningObservationConsumer
from app.models.positioning import NormalizedPositioningObservation
from app.services.friday_core import get_friday_core


class PositioningIngestionService:
    """Stage validated positioning observations without fetching or interpreting data."""

    def __init__(self, *, consumer: PositioningObservationConsumer) -> None:
        self._consumer = consumer

    def ingest(self, observation: NormalizedPositioningObservation) -> object:
        if not isinstance(observation, NormalizedPositioningObservation):
            raise TypeError("observation must be a NormalizedPositioningObservation")
        return self._consumer.ingest_positioning_observation(observation)

    def ingest_many(
        self,
        observations: tuple[NormalizedPositioningObservation, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(observation) for observation in observations)


@lru_cache
def get_positioning_ingestion_service() -> PositioningIngestionService:
    """Return the process-local boundary; no public API route uses it."""
    return PositioningIngestionService(consumer=get_friday_core())
