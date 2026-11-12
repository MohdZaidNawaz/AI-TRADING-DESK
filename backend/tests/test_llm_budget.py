from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import llm as llm_route
from app.agents import create_friday_core
from app.llm.budget import (
    BudgetPolicy,
    LLMBudgetLedger,
    ProviderPricing,
    parse_provider_pricing,
)
from app.llm.registry import LLMProviderRegistry, get_llm_provider_registry
from app.llm.strategist import ChiefStrategistLLMService
from app.models.schemas import AnalyzeRequest

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _pricing(
    provider: str = "openai", *, tier: str = "paid", input_rate: str = "1", output_rate: str = "1"
) -> ProviderPricing:
    return ProviderPricing(
        provider=provider,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        input_inr_per_million=Decimal(input_rate),
        output_inr_per_million=Decimal(output_rate),
    )


def _environment(path: Path, **updates: str) -> dict[str, str | None]:
    environment: dict[str, str | None] = {
        "OPENAI_API_KEY": None,
        "OPENAI_MODEL": None,
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_MODEL": None,
        "GEMINI_API_KEY": None,
        "GEMINI_MODEL": None,
        "FRIDAY_LLM_PROVIDER_MODE": "auto",
        "FRIDAY_LLM_DEFAULT_PROVIDER": "openai",
        "FRIDAY_LLM_MANUAL_FALLBACK_ENABLED": "false",
        "FRIDAY_LLM_TIMEOUT_SECONDS": "5",
        "FRIDAY_LLM_MAX_ATTEMPTS": "2",
        "FRIDAY_LLM_RETRY_BASE_SECONDS": "0",
        "FRIDAY_LLM_MAX_RETRY_DELAY_SECONDS": "10",
        "FRIDAY_LLM_MAX_OUTPUT_TOKENS": "900",
        "FRIDAY_LLM_BUDGET_DB_PATH": str(path),
        "FRIDAY_LLM_PROVIDER_PRICING_JSON": None,
        "FRIDAY_LLM_INR_PER_USD": None,
        "FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED": "false",
    }
    environment.update(updates)
    return environment


def test_default_policy_protects_reserve_and_requires_paid_authorization() -> None:
    policy = BudgetPolicy()
    assert policy.monthly_budget_inr == Decimal("500")
    assert policy.usable_budget_inr == Decimal("450")
    assert policy.protected_reserve_inr == Decimal("50")
    assert policy.daily_limit_inr == Decimal("25")
    assert policy.maximum_request_estimate_inr == Decimal("5")
    assert policy.paid_provider_authorized is False


def test_unknown_pricing_and_unauthorized_paid_provider_fail_before_request(
    tmp_path: Path,
) -> None:
    ledger = LLMBudgetLedger(tmp_path / "usage.sqlite3", BudgetPolicy())
    unknown = ledger.authorize(
        provider="openai", model="model", pricing=None, input_tokens=1, output_tokens=1, now=NOW
    )
    paid = ledger.authorize(
        provider="openai",
        model="model",
        pricing=_pricing(),
        input_tokens=1,
        output_tokens=1,
        now=NOW,
    )
    assert unknown.allowed is False
    assert unknown.reason == "pricing_unavailable"
    assert paid.allowed is False
    assert paid.reason == "paid_provider_not_authorized"
    assert ledger.usage() == ()


def test_unknown_pricing_blocks_provider_dispatch_and_uses_deterministic_fallback(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3", OPENAI_API_KEY="configured"
        ),
        transports={"openai": httpx.MockTransport(handler)},
    )
    request = AnalyzeRequest(query="Give me a complete overview")
    deterministic = create_friday_core().analyze(request)
    response = asyncio.run(
        ChiefStrategistLLMService(registry).enhance(request, deterministic)
    )
    assert calls == 0
    assert response.synthesis == deterministic.synthesis
    assert response.llm_synthesis.fallback_reason == "pricing_unavailable"


def test_paid_provider_requires_explicit_authorization_before_dispatch(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    pricing = json.dumps(
        {
            "openai": {
                "tier": "paid",
                "currency": "INR",
                "input_per_million": 10,
                "output_per_million": 20,
            }
        }
    )
    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3",
            OPENAI_API_KEY="configured",
            FRIDAY_LLM_PROVIDER_PRICING_JSON=pricing,
        ),
        transports={"openai": httpx.MockTransport(handler)},
    )
    request = AnalyzeRequest(query="Give me a complete overview")
    deterministic = create_friday_core().analyze(request)
    response = asyncio.run(
        ChiefStrategistLLMService(registry).enhance(request, deterministic)
    )
    assert calls == 0
    assert response.synthesis == deterministic.synthesis
    assert response.llm_synthesis.fallback_reason == "paid_provider_not_authorized"


def test_currency_conversion_requires_explicit_exchange_rate() -> None:
    configuration = json.dumps(
        {
            "openai": {
                "tier": "paid",
                "currency": "USD",
                "input_per_million": "2",
                "output_per_million": "8",
            }
        }
    )
    assert parse_provider_pricing(configuration, inr_per_usd=None) == {}
    pricing = parse_provider_pricing(configuration, inr_per_usd=Decimal("84"))["openai"]
    assert pricing.input_inr_per_million == Decimal("168")
    assert pricing.output_inr_per_million == Decimal("672")


def test_concurrent_reservations_cannot_exceed_daily_limit(tmp_path: Path) -> None:
    policy = BudgetPolicy(
        monthly_budget_inr=Decimal("500"),
        usable_budget_inr=Decimal("450"),
        protected_reserve_inr=Decimal("50"),
        daily_limit_inr=Decimal("25"),
        maximum_request_estimate_inr=Decimal("5"),
        paid_provider_authorized=True,
    )
    ledger = LLMBudgetLedger(tmp_path / "usage.sqlite3", policy)
    pricing = _pricing(input_rate="1", output_rate="5000000")

    def reserve(_: int):
        return ledger.authorize(
            provider="openai",
            model="model",
            pricing=pricing,
            input_tokens=0,
            output_tokens=1,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = tuple(executor.map(reserve, range(10)))
    assert sum(result.allowed for result in results) == 5
    assert sum(result.reason == "budget_exhausted" for result in results) == 5
    assert ledger.status(NOW).daily_committed_inr == Decimal("25")


def test_per_request_and_monthly_limits_fail_closed(tmp_path: Path) -> None:
    policy = BudgetPolicy(
        monthly_budget_inr=Decimal("10"),
        usable_budget_inr=Decimal("10"),
        protected_reserve_inr=Decimal("0"),
        daily_limit_inr=Decimal("10"),
        maximum_request_estimate_inr=Decimal("5"),
        paid_provider_authorized=True,
    )
    ledger = LLMBudgetLedger(tmp_path / "usage.sqlite3", policy)
    six_inr = _pricing(input_rate="1", output_rate="6000000")
    assert (
        ledger.authorize(
            provider="openai",
            model="model",
            pricing=six_inr,
            input_tokens=0,
            output_tokens=1,
            now=NOW,
        ).reason
        == "budget_exhausted"
    )
    five_inr = _pricing(input_rate="1", output_rate="5000000")
    results = tuple(
        ledger.authorize(
            provider="openai",
            model="model",
            pricing=five_inr,
            input_tokens=0,
            output_tokens=1,
            now=NOW,
        )
        for _ in range(3)
    )
    assert tuple(result.allowed for result in results) == (True, True, False)


def test_auto_selection_stays_on_configured_free_tier(tmp_path: Path) -> None:
    pricing = json.dumps(
        {
            "openai": {"tier": "paid", "currency": "INR", "input_per_million": 5, "output_per_million": 10},
            "anthropic": {"tier": "paid", "currency": "INR", "input_per_million": 2, "output_per_million": 3},
            "gemini": {"tier": "free", "currency": "INR", "input_per_million": 0, "output_per_million": 0},
        }
    )
    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3",
            OPENAI_API_KEY="one",
            ANTHROPIC_API_KEY="two",
            GEMINI_API_KEY="three",
            FRIDAY_LLM_PROVIDER_PRICING_JSON=pricing,
        )
    )
    candidates = registry.candidates(mode="auto", provider=None, model=None)
    assert tuple(candidate.name for candidate in candidates) == ("gemini",)


def test_auto_selection_uses_cheapest_paid_only_when_explicitly_authorized(
    tmp_path: Path,
) -> None:
    pricing = json.dumps(
        {
            "openai": {"tier": "paid", "currency": "INR", "input_per_million": 5, "output_per_million": 10},
            "anthropic": {"tier": "paid", "currency": "INR", "input_per_million": 2, "output_per_million": 3},
        }
    )
    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3",
            OPENAI_API_KEY="one",
            ANTHROPIC_API_KEY="two",
            FRIDAY_LLM_PROVIDER_PRICING_JSON=pricing,
            FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED="true",
        )
    )
    candidates = registry.candidates(mode="auto", provider=None, model=None)
    assert tuple(candidate.name for candidate in candidates) == ("anthropic", "openai")


def test_auto_selection_never_silently_falls_from_free_to_paid(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    pricing = json.dumps(
        {
            "openai": {"tier": "paid", "currency": "INR", "input_per_million": 5, "output_per_million": 10},
            "gemini": {"tier": "free", "currency": "INR", "input_per_million": 0, "output_per_million": 0},
        }
    )
    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3",
            OPENAI_API_KEY="paid",
            GEMINI_API_KEY="free",
            FRIDAY_LLM_PROVIDER_PRICING_JSON=pricing,
            FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED="true",
            FRIDAY_LLM_MAX_ATTEMPTS="1",
        ),
        transports={
            "openai": httpx.MockTransport(lambda request: calls.append("openai") or httpx.Response(200)),
            "gemini": httpx.MockTransport(lambda request: calls.append("gemini") or httpx.Response(503)),
        },
    )
    request = AnalyzeRequest(query="Give me a complete overview")
    response = asyncio.run(
        ChiefStrategistLLMService(registry).enhance(
            request, create_friday_core().analyze(request)
        )
    )
    assert calls == ["gemini"]
    assert response.llm_synthesis.fallback_reason == "provider_unavailable"


def test_usage_reconciles_reported_tokens_and_persists(tmp_path: Path) -> None:
    policy = BudgetPolicy(paid_provider_authorized=True)
    path = tmp_path / "usage.sqlite3"
    ledger = LLMBudgetLedger(path, policy)
    pricing = _pricing(input_rate="100", output_rate="200")
    authorization = ledger.authorize(
        provider="openai",
        model="model",
        pricing=pricing,
        input_tokens=1000,
        output_tokens=1000,
        now=NOW,
    )
    assert authorization.allowed
    ledger.complete(
        authorization,
        pricing=pricing,
        input_tokens=400,
        output_tokens=200,
        now=NOW,
    )
    record = LLMBudgetLedger(path, policy).usage()[0]
    assert record.input_tokens == 400
    assert record.output_tokens == 200
    assert record.estimated_cost_inr == Decimal("0.3")
    assert record.actual_cost_inr == Decimal("0.08")
    assert record.state == "completed"
    status = ledger.status(NOW)
    assert status.monthly_spent_inr == Decimal("0.08")
    assert status.pending_reserved_inr == Decimal("0")


def test_provider_failure_releases_reservation_without_evidence_ingestion(
    tmp_path: Path,
) -> None:
    calls = 0

    def failure(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    pricing = json.dumps(
        {
            "openai": {
                "tier": "free",
                "currency": "INR",
                "input_per_million": 0,
                "output_per_million": 0,
            }
        }
    )
    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3",
            OPENAI_API_KEY="configured",
            FRIDAY_LLM_PROVIDER_PRICING_JSON=pricing,
            FRIDAY_LLM_MAX_ATTEMPTS="1",
        ),
        transports={"openai": httpx.MockTransport(failure)},
    )
    request = AnalyzeRequest(query="Give me a complete overview")
    deterministic = create_friday_core().analyze(request)
    response = asyncio.run(
        ChiefStrategistLLMService(registry).enhance(request, deterministic)
    )
    assert calls == 1
    assert response.synthesis == deterministic.synthesis
    assert response.llm_synthesis.fallback_reason == "provider_unavailable"
    assert registry.budget_ledger.usage()[0].state == "released"


def test_budget_and_usage_endpoints_are_sanitized(tmp_path: Path) -> None:
    pricing = json.dumps(
        {
            "openai": {
                "tier": "free",
                "currency": "INR",
                "input_per_million": 0,
                "output_per_million": 0,
            }
        }
    )
    registry = LLMProviderRegistry(
        environment=_environment(
            tmp_path / "usage.sqlite3",
            OPENAI_API_KEY="must-not-appear",
            OPENAI_MODEL="safe-model",
            FRIDAY_LLM_PROVIDER_PRICING_JSON=pricing,
        )
    )
    authorization = registry.authorize("openai", "safe-model", input_tokens=2, output_tokens=3)
    registry.budget_ledger.complete(
        authorization,
        pricing=registry.pricing("openai"),  # type: ignore[arg-type]
        input_tokens=2,
        output_tokens=3,
    )
    application = FastAPI()
    application.include_router(llm_route.router, prefix="/api")
    application.dependency_overrides[get_llm_provider_registry] = lambda: registry
    client = TestClient(application)
    budget = client.get("/api/llm/budget")
    usage = client.get("/api/llm/usage")
    assert budget.status_code == 200
    assert budget.json()["monthly_budget_inr"] == "500"
    assert usage.status_code == 200
    assert usage.json()[0]["provider"] == "openai"
    assert "must-not-appear" not in budget.text + usage.text
