from fastapi import APIRouter

from app.data.mock_data import MARKETS
from app.models.schemas import MarketListResponse

router = APIRouter(tags=["markets"])


@router.get("/markets", response_model=MarketListResponse)
def get_markets() -> MarketListResponse:
    return MarketListResponse(
        data_source="mock",
        count=len(MARKETS),
        markets=MARKETS,
    )
