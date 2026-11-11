"""Explicit historical-dataset ingestion and read-only metadata API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.models.historical_datasets import (
    HistoricalDatasetDetailResponse,
    HistoricalDatasetIngestionRequest,
    HistoricalDatasetIngestionResponse,
    HistoricalDatasetListResponse,
)
from app.services.historical_datasets import (
    HistoricalDatasetCorruptError,
    HistoricalDatasetNotFoundError,
    HistoricalDatasetService,
    HistoricalDatasetStore,
    get_historical_dataset_service,
    get_historical_dataset_store,
)


router = APIRouter(prefix="/historical-datasets", tags=["historical-datasets"])


@router.post("/ingestions", response_model=HistoricalDatasetIngestionResponse)
async def ingest_historical_dataset(
    request: HistoricalDatasetIngestionRequest,
    service: Annotated[HistoricalDatasetService, Depends(get_historical_dataset_service)],
) -> HistoricalDatasetIngestionResponse:
    """Perform one explicit bounded provider retrieval workflow."""
    return await service.ingest(request)


@router.get("", response_model=HistoricalDatasetListResponse)
def list_historical_datasets(
    store: Annotated[HistoricalDatasetStore, Depends(get_historical_dataset_store)],
) -> HistoricalDatasetListResponse:
    return store.list()


@router.get("/{dataset_id}", response_model=HistoricalDatasetDetailResponse)
def get_historical_dataset(
    dataset_id: str,
    store: Annotated[HistoricalDatasetStore, Depends(get_historical_dataset_store)],
) -> HistoricalDatasetDetailResponse:
    try:
        return HistoricalDatasetDetailResponse(dataset=store.load(dataset_id).metadata)
    except HistoricalDatasetNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Historical dataset was not found.") from None
    except HistoricalDatasetCorruptError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Historical dataset failed integrity validation.") from None
