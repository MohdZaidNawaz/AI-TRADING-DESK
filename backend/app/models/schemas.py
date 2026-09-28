from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(APIModel):
    status: Literal["ok"]
    service: str
    version: str


class Agent(APIModel):
    id: str
    name: str
    description: str
    status: Literal["active", "syncing", "idle"]


class AgentListResponse(APIModel):
    count: int
    agents: list[Agent]


class Market(APIModel):
    symbol: str
    name: str
    price: float
    change_percent: float
    high: float
    low: float
    currency: str = "USD"
    is_mock: bool = True


class MarketListResponse(APIModel):
    data_source: Literal["mock"]
    count: int
    markets: list[Market]


class AnalyzeRequest(APIModel):
    symbol: str | None = Field(default=None, max_length=20, pattern=r"^[A-Z0-9._/-]+$")
    query: str = Field(min_length=3, max_length=500)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            normalized_symbol = value.strip().upper()
            return normalized_symbol or None
        return value

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class RoutingDecision(APIModel):
    selected_agents: list[str]
    reason: str


class AgentAnalysis(APIModel):
    agent_id: str
    agent_name: str
    status: Literal["completed", "skipped", "error"]
    summary: str
    findings: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    limitations: list[str]


class Synthesis(APIModel):
    status: Literal["completed"]
    agents_consulted: list[str]
    summary: str
    final_response: str
    caveat: str


class AnalyzeResponse(APIModel):
    analysis_id: str
    mode: Literal["deterministic_mock"]
    symbol: str | None
    query: str
    selected_agents: list[str]
    routing: RoutingDecision
    agent_results: list[AgentAnalysis]
    synthesis: Synthesis
