"""Internal provider-neutral ingestion boundary for macro-series observations."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import MacroObservationConsumer
from app.models.macro_series import NormalizedMacroObservation
from app.services.friday_core import get_friday_core


class MacroSeriesIngestionService:
    """Stage validated macro observations without fetching or interpreting data."""

    def __init__(self, *, consumer: MacroObservationConsumer) -> None:
        self._consumer = consumer

    def ingest(self, observation: NormalizedMacroObservation) -> object:
        if not isinstance(observation, NormalizedMacroObservation):
            raise TypeError("observation must be a NormalizedMacroObservation")
        return self._consumer.ingest_macro_observation(observation)

    def ingest_many(
        self,
        observations: tuple[NormalizedMacroObservation, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(observation) for observation in observations)


@lru_cache
def get_macro_series_ingestion_service() -> MacroSeriesIngestionService:
    """Return the process-local macro boundary; no public API route uses it."""
    return MacroSeriesIngestionService(consumer=get_friday_core())
