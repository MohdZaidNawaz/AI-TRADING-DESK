from fastapi import APIRouter

from app.agents import create_friday_core
from app.models.schemas import AnalyzeRequest, AnalyzeResponse

router = APIRouter(tags=["analysis"])
friday_core = create_friday_core()


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    return friday_core.analyze(request)
