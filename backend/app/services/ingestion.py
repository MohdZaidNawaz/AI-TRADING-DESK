from functools import lru_cache

from app.ingestion import IngestionService, NewsResearchIngestionService
from app.services.friday_core import get_friday_core


@lru_cache
def get_ingestion_service() -> IngestionService:
    """Return the process-local hand-off from source connectors to FRIDAY Core."""
    return IngestionService(consumer=get_friday_core())


@lru_cache
def get_news_research_ingestion_service() -> NewsResearchIngestionService:
    """Return the shared provider-neutral hand-off for normalized news evidence."""
    return NewsResearchIngestionService(consumer=get_friday_core())
