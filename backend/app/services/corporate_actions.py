"""Internal provider-neutral staging boundary for corporate-action facts."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import CorporateActionConsumer
from app.models.corporate_actions import NormalizedCorporateAction
from app.services.friday_core import get_friday_core


class CorporateActionIngestionService:
    """Stage validated corporate actions without fetching or interpreting data."""

    def __init__(self, *, consumer: CorporateActionConsumer) -> None:
        self._consumer = consumer

    def ingest(self, action: NormalizedCorporateAction) -> object:
        if not isinstance(action, NormalizedCorporateAction):
            raise TypeError("action must be a NormalizedCorporateAction")
        return self._consumer.ingest_corporate_action(action)

    def ingest_many(self, actions: tuple[NormalizedCorporateAction, ...]) -> tuple[object, ...]:
        return tuple(self.ingest(action) for action in actions)


@lru_cache
def get_corporate_action_ingestion_service() -> CorporateActionIngestionService:
    """Return the process-local boundary; no public API route uses it."""
    return CorporateActionIngestionService(consumer=get_friday_core())
