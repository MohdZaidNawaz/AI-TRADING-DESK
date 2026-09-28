from fastapi import APIRouter

from app.data.mock_data import AGENTS
from app.models.schemas import AgentListResponse

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=AgentListResponse)
def get_agents() -> AgentListResponse:
    return AgentListResponse(count=len(AGENTS), agents=AGENTS)
