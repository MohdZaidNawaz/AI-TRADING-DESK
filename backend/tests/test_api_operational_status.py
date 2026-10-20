from __future__ import annotations

import asyncio
import re

from httpx import ASGITransport, AsyncClient
import pytest
from pydantic import ValidationError

from app.main import app
from app.models.production_authorization import ProductionReadinessPrerequisite
from app.models.schemas import Market, MarketListResponse


async def _get(path: str, *, headers: dict[str, str] | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path, headers=headers)


async def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    client_address: tuple[str, int] = ("127.0.0.1", 12345),
):
    async with AsyncClient(
        transport=ASGITransport(app=app, client=client_address),
        base_url="http://127.0.0.1",
    ) as client:
        return await client.request(
            method,
            path,
            headers=headers,
            content=content,
        )


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


def test_non_loopback_client_is_rejected_before_route_handling() -> None:
    response = asyncio.run(
        _request(
            "GET",
            "/api/readiness?submission_allowed=true",
            client_address=("192.0.2.10", 41000),
        )
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Nimbus Desk API is restricted to local access."
    }
    assert re.fullmatch(r"req-[0-9a-f]{32}", response.headers["x-correlation-id"])


def test_unapproved_browser_origin_is_rejected_but_local_frontend_is_allowed() -> None:
    rejected = asyncio.run(
        _request(
            "POST",
            "/api/analyze",
            headers={"Origin": "https://untrusted.example", "Content-Type": "application/json"},
            content=b'{"query":"review market context"}',
        )
    )
    allowed = asyncio.run(
        _request(
            "POST",
            "/api/analyze",
            headers={"Origin": "http://127.0.0.1:5173", "Content-Type": "application/json"},
            content=b'{"query":"review market context"}',
        )
    )

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "Browser origin is not allowed."}
    assert allowed.status_code == 200


def test_declared_oversized_or_malformed_request_is_rejected() -> None:
    oversized = asyncio.run(
        _request(
            "POST",
            "/api/analyze",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "1048577",
            },
            content=b"{}",
        )
    )
    malformed = asyncio.run(
        _request(
            "POST",
            "/api/analyze",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "invalid",
            },
            content=b"{}",
        )
    )

    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "Request body is too large."}
    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "Content-Length header is invalid."}


def test_streamed_body_is_bounded_without_content_length() -> None:
    async def send_streamed_body():
        async def chunks():
            yield b'{' + b'"query":"' + (b"a" * 600_000)
            yield (b"b" * 600_000) + b'"}'

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://127.0.0.1",
        ) as client:
            return await client.post(
                "/api/analyze",
                headers={"Content-Type": "application/json"},
                content=chunks(),
            )

    response = asyncio.run(send_streamed_body())

    assert response.status_code == 413, response.text
    assert response.json() == {"detail": "Request body is too large."}


def test_cors_does_not_enable_browser_credentials() -> None:
    response = asyncio.run(
        _request(
            "OPTIONS",
            "/api/readiness",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize(
    "changes",
    [
        {"price": float("nan")},
        {"high": float("inf")},
        {"low": 101.0, "price": 100.0, "high": 102.0},
        {"price": 103.0, "low": 99.0, "high": 102.0},
        {"is_mock": False},
        {"symbol": "XAU USD"},
        {"currency": "US D"},
    ],
)
def test_public_market_contract_rejects_misleading_or_invalid_values(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "symbol": "XAUUSD",
        "name": "Gold / US Dollar",
        "price": 100.0,
        "change_percent": 0.5,
        "high": 101.0,
        "low": 99.0,
        "is_mock": True,
    }
    values.update(changes)

    with pytest.raises(ValidationError):
        Market.model_validate(values)


def test_public_market_list_count_must_match_mock_fixtures() -> None:
    market = Market(
        symbol="XAUUSD",
        name="Gold / US Dollar",
        price=100.0,
        change_percent=0.5,
        high=101.0,
        low=99.0,
    )

    with pytest.raises(ValidationError):
        MarketListResponse(data_source="mock", count=2, markets=[market])
