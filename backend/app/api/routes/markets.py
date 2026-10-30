from typing import Annotated

from fastapi import APIRouter, Depends

from app.models.schemas import MarketListResponse
from app.services.market_data import MarketDataService, get_market_data_service

router = APIRouter(tags=["markets"])


@router.get("/markets", response_model=MarketListResponse)
async def get_markets(
    service: Annotated[MarketDataService, Depends(get_market_data_service)],
) -> MarketListResponse:
    return await service.get_markets()
