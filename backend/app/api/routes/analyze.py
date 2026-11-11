import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.llm.registry import LLMSelectionError
from app.llm.strategist import (
    ChiefStrategistLLMService,
    get_chief_strategist_llm_service,
)
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
    llm_service: ChiefStrategistLLMService = Depends(
        get_chief_strategist_llm_service
    ),
) -> AnalyzeResponse:
    try:
        llm_service.validate_selection(request)
    except LLMSelectionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    historical_retrieval, news_retrieval = await asyncio.gather(
        historical_service.prepare(request),
        news_service.prepare(request),
    )
    deterministic = friday_core.analyze(
        request,
        historical_retrieval=historical_retrieval,
        news_retrieval=news_retrieval,
    )
    return await llm_service.enhance(request, deterministic)
