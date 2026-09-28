"""Dependency wiring for Twelve Data's provider-specific client."""

from fastapi import HTTPException, status

from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConfigurationError,
    twelve_data_connector,
)
from app.ingestion import MarketSnapshotService
from app.services.ingestion import get_ingestion_service


def get_twelve_data_snapshot_service() -> MarketSnapshotService:
    """Build the shared snapshot workflow from environment-backed credentials."""
    try:
        client = TwelveDataClient.from_environment()
    except TwelveDataConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Twelve Data is not configured.",
        ) from None

    return MarketSnapshotService(
        client=client,
        connector=twelve_data_connector,
        ingestion=get_ingestion_service(),
    )
