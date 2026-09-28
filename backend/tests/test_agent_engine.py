import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from app.main import app


def post_json(path: str, payload: Mapping[str, Any]) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(path, json=payload)

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


def test_existing_health_endpoint_still_works() -> None:
    response = get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_generic_analysis_routes_to_full_desk() -> None:
    response = post_json(
        "/api/analyze",
        {"symbol": "xauusd", "query": "Give me a complete overview"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "XAUUSD"
    assert payload["mode"] == "deterministic_mock"
    assert len(payload["routing"]["selected_agents"]) == 8
    assert len(payload["agent_results"]) == 8
    assert all(result["status"] == "completed" for result in payload["agent_results"])
    assert "No live data" in payload["synthesis"]["caveat"]


def test_targeted_query_routes_specialists_and_marks_others_skipped() -> None:
    response = post_json(
        "/api/analyze",
        {"symbol": "EURUSD", "query": "Review the latest news catalysts"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["routing"]["selected_agents"] == ["news", "risk", "journal"]

    statuses = {
        result["agent_id"]: result["status"]
        for result in payload["agent_results"]
    }
    assert statuses["news"] == "completed"
    assert statuses["risk"] == "completed"
    assert statuses["journal"] == "completed"
    assert statuses["technical"] == "skipped"


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
