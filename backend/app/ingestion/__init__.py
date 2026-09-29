"""Provider-neutral ingestion contracts and services."""

from app.ingestion.service import (
    IngestionService,
    MarketSnapshotService,
    NewsResearchIngestionService,
)

__all__ = [
    "IngestionService",
    "MarketSnapshotService",
    "NewsResearchIngestionService",
]
