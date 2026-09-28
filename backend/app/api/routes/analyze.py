from fastapi import APIRouter

from app.agents import create_orchestrator
from app.models.schemas import AnalyzeRequest, AnalyzeResponse

router = APIRouter(tags=["analysis"])
orchestrator = create_orchestrator()


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    return orchestrator.analyze(request)
