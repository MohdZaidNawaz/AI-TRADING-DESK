import asyncio

from fastapi import APIRouter, Depends

from app.models.schemas import AnalyzeRequest, AnalyzeResponse
from app.services.friday_core import get_friday_core
from app.services.twelve_data_historical import (
    TwelveDataHistoricalService,
    get_twelve_data_historical_service,
)
from app.services.news_intelligence import (
    GdeltNewsIntelligenceRetrievalService,
    get_news_intelligence_retrieval_service,
)

router = APIRouter(tags=["analysis"])
friday_core = get_friday_core()


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: AnalyzeRequest,
    historical_service: TwelveDataHistoricalService = Depends(
        get_twelve_data_historical_service
    ),
    news_service: GdeltNewsIntelligenceRetrievalService = Depends(
        get_news_intelligence_retrieval_service
    ),
) -> AnalyzeResponse:
    historical_retrieval, news_retrieval = await asyncio.gather(
        historical_service.prepare(request),
        news_service.prepare(request),
    )
    return friday_core.analyze(
        request,
        historical_retrieval=historical_retrieval,
        news_retrieval=news_retrieval,
    )
