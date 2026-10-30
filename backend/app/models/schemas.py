import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class WebhookAcceptedResponse(APIModel):
    status: Literal["accepted"]
    event_id: str
    source: Literal["tradingview"]
