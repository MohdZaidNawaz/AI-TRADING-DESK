"""Manual Twelve Data snapshot endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.connectors.twelve_data import (
    TwelveDataPayloadError,
    TwelveDataProviderError,
)
from app.ingestion import MarketSnapshotService
from app.models.events import NormalizedDataEvent
from app.services.twelve_data import get_twelve_data_snapshot_service

router = APIRouter(prefix="/market-data/twelve-data", tags=["market-data"])


@router.post("/snapshot", response_model=NormalizedDataEvent)
async def request_twelve_data_snapshot(
    service: Annotated[MarketSnapshotService, Depends(get_twelve_data_snapshot_service)],
    symbol: Annotated[
        str,
        Query(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9._/-]+$"),
    ] = "XAU/USD",
) -> NormalizedDataEvent:
    """Fetch one provider snapshot and ingest its normalized event into FRIDAY."""
    try:
        return await service.snapshot(symbol.strip().upper())
    except TwelveDataProviderError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Twelve Data request failed.",
        ) from None
    except TwelveDataPayloadError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Twelve Data returned malformed market data.",
        ) from None
