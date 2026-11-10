"""Read-only instrument catalogue API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models.instruments import (
    InstrumentCatalogueInstrumentResponse,
    InstrumentCatalogueSearchResponse,
    InstrumentCatalogueStatus,
    InstrumentCategory,
)
from app.services.instrument_catalogue import (
    InstrumentCatalogueService,
    InstrumentCatalogueUnavailable,
    get_instrument_catalogue_service,
)

router = APIRouter(tags=["instruments"])


@router.get("/instruments", response_model=InstrumentCatalogueSearchResponse)
async def list_instruments(
    service: Annotated[
        InstrumentCatalogueService, Depends(get_instrument_catalogue_service)
    ],
    q: str | None = Query(default=None, max_length=120),
    category: InstrumentCategory | None = None,
    forex_group: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> InstrumentCatalogueSearchResponse:
    try:
        return await service.list_instruments(
            query=q,
            category=category,
            forex_group=forex_group,
            page=page,
            limit=limit,
        )
    except InstrumentCatalogueUnavailable:
        raise HTTPException(status_code=503, detail="Instrument catalogue unavailable.") from None


@router.get(
    "/instruments/status", response_model=InstrumentCatalogueStatus
)
async def instrument_catalogue_status(
    service: Annotated[
        InstrumentCatalogueService, Depends(get_instrument_catalogue_service)
    ],
) -> InstrumentCatalogueStatus:
    return await service.get_status()


@router.get(
    "/instruments/{instrument_id:path}",
    response_model=InstrumentCatalogueInstrumentResponse,
)
async def get_instrument(
    instrument_id: str,
    service: Annotated[
        InstrumentCatalogueService, Depends(get_instrument_catalogue_service)
    ],
) -> InstrumentCatalogueInstrumentResponse:
    try:
        response = await service.get_instrument(instrument_id)
    except InstrumentCatalogueUnavailable:
        raise HTTPException(status_code=503, detail="Instrument catalogue unavailable.") from None
    if response is None:
        raise HTTPException(status_code=404, detail="Instrument not found.")
    return response
