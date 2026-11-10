import math
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.research_requests import ResearchRequestContext
from app.models.agent_coordination import (
    AgentCoordinationReport,
    AgentEvidenceLink,
    AgentExecutionState,
    AgentOutputKind,
)

if TYPE_CHECKING:
    from app.models.liquidity_intelligence import LiquidityIntelligenceReport
    from app.models.macro_intelligence import MacroIntelligenceReport
    from app.models.news_intelligence import NewsEconomicIntelligenceReport
    from app.models.risk_strategy_intelligence import RiskStrategyIntelligenceReport
    from app.models.technical_analysis import TechnicalAnalysisReport


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
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9._/-]+$")
    name: str = Field(min_length=1, max_length=120)
    price: float
    change_percent: float
    high: float
    low: float
    currency: str = Field(default="USD", min_length=3, max_length=8, pattern=r"^[A-Z0-9]+$")
    is_mock: bool = True
    data_source: Literal["mock", "twelve_data"] = "mock"
    quote_timestamp: datetime | None = None
    received_at: datetime | None = None
    quote_age_seconds: float | None = None
    provider_is_delayed: bool | None = None
    freshness: Literal["live", "delayed", "stale", "mock"] = "mock"
    freshness_reason: Literal[
        "fresh_quote",
        "quote_age",
        "provider_delayed",
        "stale_quote_age",
        "market_closed",
        "mock",
    ] = "mock"
    is_market_open: bool | None = None

    @field_validator("symbol", "currency", mode="before")
    @classmethod
    def canonical_market_identifier(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("price", "change_percent", "high", "low", mode="before")
    @classmethod
    def finite_market_number(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("market values must be finite numbers")
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError("market values must be finite numbers") from error
        if not math.isfinite(parsed):
            raise ValueError("market values must be finite numbers")
        return parsed

    @model_validator(mode="after")
    def coherent_mock_quote(self) -> "Market":
        if self.price <= 0 or self.high <= 0 or self.low <= 0:
            raise ValueError("market prices must be positive")
        if not self.low <= self.price <= self.high:
            raise ValueError("mock market price must be inside the displayed range")
        if self.data_source == "mock" and not self.is_mock:
            raise ValueError("mock data source must set is_mock=True")
        if self.data_source == "twelve_data" and self.is_mock:
            raise ValueError("twelve_data source must set is_mock=False")
        return self


class MarketListResponse(APIModel):
    data_source: Literal["mock", "twelve_data"]
    count: int = Field(ge=0)
    markets: list[Market]

    @model_validator(mode="after")
    def count_matches_mock_values(self) -> "MarketListResponse":
        if self.count != len(self.markets):
            raise ValueError("market count must match the mock fixture list")
        if any(m.data_source != self.data_source for m in self.markets):
            raise ValueError("all markets must match the response data_source")
        if any(m.is_mock != (self.data_source == "mock") for m in self.markets):
            raise ValueError("market is_mock flag must match response data_source")
        return self


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
    research_context: ResearchRequestContext | None = None


class AgentAnalysis(APIModel):
    agent_id: str
    agent_name: str
    status: Literal["completed", "skipped", "error"]
    summary: str
    findings: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    limitations: list[str]
    execution_state: AgentExecutionState | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence_references: list[AgentEvidenceLink] = Field(default_factory=list)
    calculation_methods: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    output_kinds: list[AgentOutputKind] = Field(default_factory=list)


class Synthesis(APIModel):
    status: Literal["completed"]
    agents_consulted: list[str]
    summary: str
    final_response: str
    caveat: str
    agreement_agent_ids: list[str] = Field(default_factory=list)
    disagreement_agent_ids: list[str] = Field(default_factory=list)
    missing_agent_ids: list[str] = Field(default_factory=list)
    evidence_references: list[AgentEvidenceLink] = Field(default_factory=list)
    conditional_scenarios: list[str] = Field(default_factory=list)


class AnalyzeResponse(APIModel):
    analysis_id: str
    mode: Literal["deterministic_mock"]
    symbol: str | None
    query: str
    selected_agents: list[str]
    routing: RoutingDecision
    agent_results: list[AgentAnalysis]
    synthesis: Synthesis
    coordination: AgentCoordinationReport | None = None
    technical_analysis: "TechnicalAnalysisReport | None" = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    news_intelligence: "NewsEconomicIntelligenceReport | None" = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    macro_intelligence: "MacroIntelligenceReport | None" = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    liquidity_intelligence: "LiquidityIntelligenceReport | None" = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    risk_strategy_intelligence: "RiskStrategyIntelligenceReport | None" = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class WebhookAcceptedResponse(APIModel):
    status: Literal["accepted"]
    event_id: str
    source: Literal["tradingview"]
