from fastapi import APIRouter

from app.models.schemas import AnalyzeRequest, AnalyzeResponse
from app.services.friday_core import get_friday_core

router = APIRouter(tags=["analysis"])
friday_core = get_friday_core()


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    return friday_core.analyze(request)
