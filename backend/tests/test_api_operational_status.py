from __future__ import annotations

import asyncio
import re

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.production_authorization import ProductionReadinessPrerequisite


async def _get(path: str, *, headers: dict[str, str] | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path, headers=headers)


def test_liveness_response_includes_generated_correlation_id() -> None:
    response = asyncio.run(_get("/api/health"))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "nimbus-desk-api",
        "version": "0.2.0",
    }
    assert response.headers["cache-control"] == "no-store"
    assert re.fullmatch(r"req-[0-9a-f]{32}", response.headers["x-correlation-id"])


def test_safe_correlation_id_is_echoed_and_invalid_value_is_replaced() -> None:
    accepted = asyncio.run(
        _get("/api/health", headers={"X-Correlation-ID": "desk.request-42"})
    )
    replaced = asyncio.run(
        _get("/api/health", headers={"X-Correlation-ID": "invalid value\n"})
    )

    assert accepted.headers["x-correlation-id"] == "desk.request-42"
    assert replaced.headers["x-correlation-id"] != "invalid value\n"
    assert re.fullmatch(r"req-[0-9a-f]{32}", replaced.headers["x-correlation-id"])


def test_readiness_is_complete_and_structurally_non_authorizing() -> None:
    response = asyncio.run(_get("/api/readiness"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["production_authorization_available"] is False
    assert payload["submission_allowed"] is False
    assert payload["execution_authority"] is False
    assert {item["prerequisite"] for item in payload["checks"]} == {
        item.value for item in ProductionReadinessPrerequisite
    }
    assert all(item["authorizes_execution"] is False for item in payload["checks"])
    assert "LIVE_AND_DEMO_SUBMISSION_DISABLED" in payload["reason_codes"]
    assert "INDEPENDENT_TRUST_ANCHOR_UNAVAILABLE" in payload["reason_codes"]


def test_readiness_does_not_accept_caller_supplied_authority() -> None:
    response = asyncio.run(
        _get(
            "/api/readiness?submission_allowed=true&execution_authority=true",
            headers={"X-Correlation-ID": "readiness-check"},
        )
    )

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "readiness-check"
    assert response.json()["submission_allowed"] is False
    assert response.json()["execution_authority"] is False
