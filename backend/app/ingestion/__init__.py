"""Provider-neutral ingestion contracts and services."""

from app.ingestion.service import (
    IngestionService,
    MarketSnapshotService,
    NewsResearchIngestionService,
)
from app.ingestion.contracts import EconomicEventConsumer, NewsEvidenceConsumer

__all__ = [
    "IngestionService",
    "MarketSnapshotService",
    "NewsResearchIngestionService",
    "NewsEvidenceConsumer",
    "EconomicEventConsumer",
]
