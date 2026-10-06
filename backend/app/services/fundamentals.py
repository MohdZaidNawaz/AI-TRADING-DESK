"""Internal provider-neutral ingestion boundary for financial observations."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import FundamentalObservationConsumer
from app.models.fundamentals import NormalizedFundamentalObservation
from app.services.friday_core import get_friday_core


class FundamentalIngestionService:
    """Stage validated financial observations without fetching or interpreting data."""

    def __init__(self, *, consumer: FundamentalObservationConsumer) -> None:
        self._consumer = consumer

    def ingest(self, observation: NormalizedFundamentalObservation) -> object:
        if not isinstance(observation, NormalizedFundamentalObservation):
            raise TypeError("observation must be a NormalizedFundamentalObservation")
        return self._consumer.ingest_fundamental_observation(observation)

    def ingest_many(
        self,
        observations: tuple[NormalizedFundamentalObservation, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(observation) for observation in observations)


@lru_cache
def get_fundamental_ingestion_service() -> FundamentalIngestionService:
    """Return the process-local boundary; no public API route uses it."""
    return FundamentalIngestionService(consumer=get_friday_core())
