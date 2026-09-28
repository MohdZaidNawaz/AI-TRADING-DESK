from functools import lru_cache

from app.ingestion import IngestionService
from app.services.friday_core import get_friday_core


@lru_cache
def get_ingestion_service() -> IngestionService:
    """Return the process-local hand-off from source connectors to FRIDAY Core."""
    return IngestionService(consumer=get_friday_core())
