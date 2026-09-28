"""Provider-neutral ingestion contracts and services."""

from app.ingestion.service import IngestionService, MarketSnapshotService

__all__ = ["IngestionService", "MarketSnapshotService"]
