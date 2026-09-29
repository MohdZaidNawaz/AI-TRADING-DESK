import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from app.agents import create_friday_core
from app.agents.master import MasterAgent
from app.agents.specialists import create_specialist_agents
from app.connectors import tradingview_connector
from app.ingestion import IngestionService
from app.ingestion.contracts import SourceConnector
from app.main import app
from app.models.events import EventType, NormalizedDataEvent, SourceTransport
from app.webhooks.tradingview import TradingViewPayloadError, normalize_tradingview_payload


def post_json(
    path: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(path, json=payload, headers=headers)

    return asyncio.run(send_request())


def get(path: str) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return asyncio.run(send_request())


def post_content(
    path: str,
    content: str,
    headers: Mapping[str, str],
) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(path, content=content, headers=headers)

    return asyncio.run(send_request())


def test_existing_health_endpoint_still_works() -> None:
    response = get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_existing_agents_and_markets_endpoints_still_work() -> None:
    agents_response = get("/api/agents")
    markets_response = get("/api/markets")

    assert agents_response.status_code == 200
    agents_payload = agents_response.json()
    assert agents_payload["count"] == 9
    assert [(agent["id"], agent["name"]) for agent in agents_payload["agents"]] == [
        ("atlas_macro", "Atlas Macro"),
        ("capitol_smart_money", "Capitol Smart Money"),
        ("scout_recon", "Scout Recon"),
        ("athena_analyst", "Athena Analyst"),
        ("chartist_technician", "Chartist Technician"),
        ("oracle_quant", "Oracle Quant"),
        ("sentinel_risk_officer", "Sentinel Risk Officer"),
        ("pilot_execution", "Pilot Execution"),
        ("ledger_the_book", "Ledger The Book"),
    ]
    assert "news" not in {agent["id"] for agent in agents_payload["agents"]}
    assert "master" not in {agent["id"] for agent in agents_payload["agents"]}
    assert markets_response.status_code == 200
    assert markets_response.json()["count"] == 3


def test_friday_is_the_master_and_not_one_of_the_nine_specialist_seats() -> None:
    specialists = create_specialist_agents()

    assert MasterAgent.name == "F.R.I.D.A.Y. — The Master"
    assert len(specialists) == 9
    assert MasterAgent.agent_id not in {agent.agent_id for agent in specialists}


def test_pilot_execution_is_a_non_executing_placeholder() -> None:
    response = post_json(
        "/api/analyze",
        {"symbol": "XAUUSD", "query": "Review a broker execution order"},
    )

    assert response.status_code == 200
    pilot_result = next(
        result for result in response.json()["agent_results"] if result["agent_id"] == "pilot_execution"
    )
    assert pilot_result["status"] == "completed"
    assert "disabled" in pilot_result["summary"].lower()
    assert "not authorized" in pilot_result["summary"].lower()
    assert "mt5" in pilot_result["limitations"][0].lower()
    assert "order placement" in pilot_result["limitations"][0].lower()


def test_generic_analysis_routes_to_full_desk() -> None:
    response = post_json(
        "/api/analyze",
        {"symbol": "xauusd", "query": "Give me a complete overview"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "XAUUSD"
    assert payload["mode"] == "deterministic_mock"
    assert payload["selected_agents"] == payload["routing"]["selected_agents"]
    assert len(payload["routing"]["selected_agents"]) == 9
    assert len(payload["agent_results"]) == 9
    assert all(result["status"] == "completed" for result in payload["agent_results"])
    assert "No live data" in payload["synthesis"]["caveat"]


def test_targeted_query_routes_specialists_and_marks_others_skipped() -> None:
    response = post_json(
        "/api/analyze",
        {"symbol": "EURUSD", "query": "Review the latest news catalysts"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_agents"] == [
        "scout_recon",
        "sentinel_risk_officer",
        "ledger_the_book",
    ]
    assert payload["routing"]["selected_agents"] == [
        "scout_recon",
        "sentinel_risk_officer",
        "ledger_the_book",
    ]

    statuses = {
        result["agent_id"]: result["status"]
        for result in payload["agent_results"]
    }
    assert statuses["scout_recon"] == "completed"
    assert statuses["sentinel_risk_officer"] == "completed"
    assert statuses["ledger_the_book"] == "completed"
    assert statuses["chartist_technician"] == "skipped"
    assert payload["synthesis"]["agents_consulted"] == [
        "scout_recon",
        "sentinel_risk_officer",
        "ledger_the_book",
    ]


def test_analysis_accepts_an_optional_symbol() -> None:
    response = post_json(
        "/api/analyze",
        {"query": "Review the latest news catalysts"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] is None
    assert payload["selected_agents"] == [
        "scout_recon",
        "sentinel_risk_officer",
        "ledger_the_book",
    ]
    assert "MARKET CONTEXT" in payload["synthesis"]["summary"]


def test_same_request_has_deterministic_response() -> None:
    request = {"symbol": "BTCUSD", "query": "Technical momentum and risk"}
    first = post_json("/api/analyze", request).json()
    second = post_json("/api/analyze", request).json()

    assert first == second


def test_invalid_analysis_request_is_rejected() -> None:
    response = post_json(
        "/api/analyze",
        {"symbol": "", "query": ""},
    )

    assert response.status_code == 422


def test_tradingview_webhook_accepts_a_valid_authenticated_alert(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "test-webhook-token")

    response = post_json(
        "/api/webhooks/tradingview/test-webhook-token",
        {
            "symbol": "OANDA:XAUUSD",
            "timeframe": "60",
            "event": "price_alert",
            "price": "2648.32",
            "time": "2026-09-29T12:00:00Z",
            "open": "2640.00",
            "high": "2650.00",
            "low": "2638.00",
            "close": "2648.32",
            "volume": "1420",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["source"] == "tradingview"
    assert response.json()["event_id"].startswith("tv-")


def test_tradingview_connector_normalizes_a_rich_alert_payload() -> None:
    event = normalize_tradingview_payload(
        {
            "symbol": "OANDA:XAUUSD",
            "timeframe": "60",
            "event": "price_alert",
            "alert_name": "Gold momentum review",
            "message": "Momentum threshold observed.",
            "price": "2648.32",
            "open": "2640.00",
            "high": "2650.00",
            "low": "2638.00",
            "close": "2648.32",
            "volume": "1420",
            "time": "2026-09-29T12:00:00Z",
            "indicators": {"momentum_score": "61.2"},
            "plots": {"trend_line": "2639.5"},
            "indicator_values": {"custom_signal": "1"},
        }
    )

    assert isinstance(event, NormalizedDataEvent)
    assert event.exchange == "OANDA"
    assert event.symbol == "XAUUSD"
    assert event.timeframe == "60"
    assert event.alert_name == "Gold momentum review"
    assert event.message == "Momentum threshold observed."
    assert event.open == 2640.0
    assert event.high == 2650.0
    assert event.low == 2638.0
    assert event.close == 2648.32
    assert event.volume == 1420.0
    assert event.indicator_values == {
        "momentum_score": 61.2,
        "trend_line": 2639.5,
        "custom_signal": 1.0,
    }
    assert event.source.source_id == "tradingview"


def test_tradingview_connector_allows_missing_optional_rich_fields() -> None:
    event = normalize_tradingview_payload({"symbol": "XAUUSD", "price": "2648.32"})

    assert event.alert_name is None
    assert event.message is None
    assert event.indicator_values == {}
    assert event.open is None
    assert event.volume is None


def test_tradingview_webhook_rejects_malformed_numeric_values(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "test-webhook-token")

    response = post_json(
        "/api/webhooks/tradingview/test-webhook-token",
        {"symbol": "XAUUSD", "price": "not-a-number"},
    )

    assert response.status_code == 422


def test_tradingview_connector_rejects_malformed_indicator_values() -> None:
    with pytest.raises(TradingViewPayloadError, match="indicators.momentum_score must be numeric"):
        normalize_tradingview_payload(
            {
                "symbol": "XAUUSD",
                "indicators": {"momentum_score": "not-a-number"},
            }
        )


def test_tradingview_webhook_rejects_invalid_authentication(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "test-webhook-token")

    response = post_json(
        "/api/webhooks/tradingview/incorrect-token",
        {"symbol": "XAUUSD"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook authentication."}
    assert "test-webhook-token" not in response.text


def test_tradingview_webhook_rejects_a_non_ascii_token(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "test-webhook-token")

    response = post_json(
        "/api/webhooks/tradingview/%E2%9C%93",
        {"symbol": "XAUUSD"},
    )

    assert response.status_code == 401


def test_tradingview_webhook_rejects_missing_secret_configuration(monkeypatch: Any) -> None:
    monkeypatch.delenv("TRADINGVIEW_WEBHOOK_SECRET", raising=False)

    response = post_json(
        "/api/webhooks/tradingview/a-token-value",
        {"symbol": "XAUUSD"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "TradingView webhook authentication is not configured."}


def test_tradingview_webhook_rejects_malformed_json(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "test-webhook-token")

    response = post_content(
        "/api/webhooks/tradingview/test-webhook-token",
        "{not-json}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_tradingview_webhook_rejects_missing_symbol(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "test-webhook-token")

    response = post_json(
        "/api/webhooks/tradingview/test-webhook-token",
        {"price": "2648.32"},
    )

    assert response.status_code == 422


def test_tradingview_placeholder_template_values_are_normalized() -> None:
    event = normalize_tradingview_payload(
        {
            "symbol": "{{exchange}}:{{ticker}}",
            "exchange": "OANDA",
            "ticker": "XAUUSD",
            "price": "{{close}}",
            "close": "2648.32",
            "time": "{{time}}",
            "timestamp": "2026-09-29T12:00:00Z",
            "open": "2640.00",
            "high": "2650.00",
            "low": "2638.00",
            "volume": "1420",
        }
    )

    assert event.exchange == "OANDA"
    assert event.symbol == "XAUUSD"
    assert event.source.source_id == "tradingview"
    assert event.source.transport is SourceTransport.WEBHOOK
    assert event.event_type is EventType.MARKET_ALERT
    assert event.price == 2648.32
    assert event.close == 2648.32
    assert event.timestamp.isoformat() == "2026-09-29T12:00:00+00:00"


def test_tradingview_event_reuses_friday_core_analysis_flow() -> None:
    event = normalize_tradingview_payload(
        {"symbol": "XAUUSD", "event": "price_alert", "price": "2648.32"}
    )
    core = create_friday_core()

    analysis = core.ingest_market_event(event)

    assert core.recent_market_events == (event,)
    assert analysis.symbol == "XAUUSD"
    assert "chartist_technician" in analysis.selected_agents
    assert "sentinel_risk_officer" in analysis.selected_agents


def test_tradingview_connector_implements_the_provider_contract() -> None:
    assert isinstance(tradingview_connector, SourceConnector)
    assert tradingview_connector.source.source_id == "tradingview"


def test_ingestion_service_hands_normalized_events_to_friday_core() -> None:
    event = normalize_tradingview_payload(
        {"symbol": "XAUUSD", "event": "price_alert", "price": "2648.32"}
    )
    core = create_friday_core()
    ingestion = IngestionService(consumer=core)

    analysis = ingestion.ingest(event)

    assert core.recent_market_events == (event,)
    assert analysis.symbol == "XAUUSD"
