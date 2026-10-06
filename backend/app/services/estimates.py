"""Internal provider-neutral ingestion boundary for analyst estimates."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import EstimateObservationConsumer
from app.models.estimates import NormalizedEstimateObservation
from app.services.friday_core import get_friday_core


class EstimateIngestionService:
    """Stage validated estimates without fetching, calculating, or interpreting them."""

    def __init__(self, *, consumer: EstimateObservationConsumer) -> None:
        self._consumer = consumer

    def ingest(self, observation: NormalizedEstimateObservation) -> object:
        if not isinstance(observation, NormalizedEstimateObservation):
            raise TypeError("observation must be a NormalizedEstimateObservation")
        return self._consumer.ingest_estimate_observation(observation)

    def ingest_many(
        self,
        observations: tuple[NormalizedEstimateObservation, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(observation) for observation in observations)


@lru_cache
def get_estimate_ingestion_service() -> EstimateIngestionService:
    """Return the process-local boundary; no public API route uses it."""
    return EstimateIngestionService(consumer=get_friday_core())
