"""Offline acceptance coverage for deterministic analysis plus optional LLM synthesis."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
import pytest

from app.agents import create_friday_core
from app.api.routes import analyze as analyze_route
from app.api.routes import llm as llm_route
from app.llm.registry import LLMProviderRegistry, get_llm_provider_registry
from app.llm.strategist import ChiefStrategistLLMService, get_chief_strategist_llm_service
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
)
from app.models.schemas import AnalyzeRequest
from app.services.news_intelligence import get_news_intelligence_retrieval_service
from app.services.twelve_data_historical import get_twelve_data_historical_service


class _NoExternalRetrieval:
    async def prepare(self, request: AnalyzeRequest) -> None:
        del request
        return None


def _bar(index: int, *, observed_at: datetime) -> NormalizedMarketBar:
    opened_at = observed_at - timedelta(minutes=15 * (40 - index))
    closed_at = opened_at + timedelta(minutes=15)
    close = 2300.0 + index
    return NormalizedMarketBar(
        source=MarketSeriesSource(
            source_id="phase6b6_synthetic",
            display_name="Phase 6B.6 synthetic fixture",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        ),
        symbol="XAU/USD",
        asset_class=AssetClass.COMMODITY,
        venue="TEST",
        timeframe="15m",
        opened_at=opened_at,
        closed_at=closed_at,
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000.0 + index,
        volume_kind=VolumeKind.TICK,
        state=MarketBarState.COMPLETED,
        received_at=closed_at,
    )


def _narrative() -> dict[str, object]:
    return {
        "executive_summary": "The verified evidence remains limited and does not support execution.",
        "analytical_stance": "insufficient_data",
        "supporting_points": [],
        "counterpoints": [],
        "conditional_scenarios": [],
        "invalidation_conditions": [],
        "missing_evidence": ["Independent live execution evidence is unavailable."],
        "limitations": ["This is explanatory narrative only."],
        "evidence_reference_ids": [],
    }


def _environment(path: Path) -> dict[str, str | None]:
    return {
        "OPENAI_API_KEY": "acceptance-secret-key",
        "OPENAI_MODEL": "acceptance-model",
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_MODEL": None,
        "GEMINI_API_KEY": None,
        "GEMINI_MODEL": None,
        "FRIDAY_LLM_PROVIDER_MODE": "auto",
        "FRIDAY_LLM_DEFAULT_PROVIDER": "openai",
        "FRIDAY_LLM_MANUAL_FALLBACK_ENABLED": "false",
        "FRIDAY_LLM_TIMEOUT_SECONDS": "5",
        "FRIDAY_LLM_MAX_ATTEMPTS": "1",
        "FRIDAY_LLM_RETRY_BASE_SECONDS": "0",
        "FRIDAY_LLM_MAX_RETRY_DELAY_SECONDS": "1",
        "FRIDAY_LLM_MAX_OUTPUT_TOKENS": "900",
        "FRIDAY_LLM_BUDGET_DB_PATH": str(path),
        "FRIDAY_LLM_PROVIDER_PRICING_JSON": json.dumps(
            {
                "openai": {
                    "tier": "free",
                    "currency": "INR",
                    "input_per_million": 0,
                    "output_per_million": 0,
                }
            }
        ),
        "FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED": "false",
    }


def test_complete_offline_analysis_budget_and_usage_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = create_friday_core()
    observed_at = datetime.now(UTC).replace(microsecond=0)
    for index in range(40):
        core.ingest_market_bar(_bar(index, observed_at=observed_at))

    provider_calls = 0
    captured_payload: dict[str, object] = {}
    registry: LLMProviderRegistry

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        captured_payload.update(json.loads(request.content))
        usage = registry.budget_ledger.usage()
        assert len(usage) == 1
        assert usage[0].state == "reserved"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(_narrative())}
                        ],
                    }
                ],
                "usage": {"input_tokens": 400, "output_tokens": 120},
            },
        )

    registry = LLMProviderRegistry(
        environment=_environment(tmp_path / "usage.sqlite3"),
        transports={"openai": httpx.MockTransport(provider)},
    )
    application = FastAPI()
    application.include_router(analyze_route.router, prefix="/api")
    application.include_router(llm_route.router, prefix="/api")
    monkeypatch.setattr(analyze_route, "friday_core", core)
    application.dependency_overrides[get_chief_strategist_llm_service] = (
        lambda: ChiefStrategistLLMService(registry)
    )
    application.dependency_overrides[get_llm_provider_registry] = lambda: registry
    application.dependency_overrides[get_twelve_data_historical_service] = _NoExternalRetrieval
    application.dependency_overrides[get_news_intelligence_retrieval_service] = _NoExternalRetrieval

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://acceptance.test",
        ) as client:
            analysis = await client.post(
                "/api/analyze",
                json={"query": "Give me a complete XAUUSD analysis on 15m"},
            )
            budget = await client.get("/api/llm/budget")
            usage = await client.get("/api/llm/usage")
            return analysis, budget, usage

    analysis, budget, usage = asyncio.run(exercise())
    assert analysis.status_code == 200
    body = analysis.json()
    assert provider_calls == 1
    assert len(body["agent_results"]) == 9
    assert all(result["status"] == "completed" for result in body["agent_results"])
    assert any(result["evidence_references"] for result in body["agent_results"])
    assert body["llm_synthesis"]["status"] == "llm_synthesis"
    assert body["llm_synthesis"]["provider"] == "openai"
    assert body["llm_synthesis"]["analytical_stance"] == "insufficient_data"
    assert body["mode"] == "deterministic_mock"
    assert body["coordination"] is not None

    provider_input = str(captured_payload["input"])
    assert provider_input.startswith("<untrusted_evidence_json>")
    assert provider_input.endswith("</untrusted_evidence_json>")
    assert "acceptance-secret-key" not in provider_input
    assert "authorization" not in provider_input.casefold()

    assert budget.status_code == 200
    assert budget.json()["paid_provider_authorized"] is False
    assert budget.json()["pending_request_count"] == 0
    assert usage.status_code == 200
    assert len(usage.json()) == 1
    assert usage.json()[0]["state"] == "completed"
    assert usage.json()[0]["input_tokens"] == 400
    assert usage.json()[0]["output_tokens"] == 120
    serialized = analysis.text + budget.text + usage.text
    assert "acceptance-secret-key" not in serialized
    assert "system_prompt" not in serialized
    assert "user_prompt" not in serialized
