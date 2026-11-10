"""Shared bounded HTTP transport for macro providers; it never retries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
import ssl
from typing import Any

import httpx

from app.models.macro_providers import MacroProviderFailureCategory


MAX_RESPONSE_BYTES = 2_000_000


class MacroProviderError(RuntimeError):
    """Safe provider failure without request URLs, bodies, headers, or credentials."""

    def __init__(
        self,
        category: MacroProviderFailureCategory,
        *,
        reason_code: str,
        failure_stage: str,
        request_sent: bool,
        http_status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.category = category
        self.reason_code = reason_code
        self.failure_stage = failure_stage
        self.request_sent = request_sent
        self.http_status_code = http_status_code
        self.retry_after_seconds = retry_after_seconds


ClientFactory = Callable[..., httpx.AsyncClient]


class MacroHttpTransport:
    """One-request transport with explicit TLS, timeout, and environment isolation."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        self._timeout = httpx.Timeout(
            connect=timeout_seconds,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )
        self._client_factory = client_factory
        self._ssl_context = ssl.create_default_context()

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self._get(url, params=params, headers=headers)
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise MacroProviderError(
                MacroProviderFailureCategory.MALFORMED_RESPONSE,
                reason_code="INVALID_JSON",
                failure_stage="response_decode",
                request_sent=True,
                http_status_code=response.status_code,
            ) from None

    async def get_xml(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        response = await self._get(url, params=params, headers=headers)
        return bytes(response.content)

    async def post_json(
        self,
        url: str,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self._request(
            "POST", url, json=payload, headers=headers
        )
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            raise MacroProviderError(
                MacroProviderFailureCategory.MALFORMED_RESPONSE,
                reason_code="INVALID_JSON",
                failure_stage="response_decode",
                request_sent=True,
                http_status_code=response.status_code,
            ) from None

    async def _get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        return await self._request("GET", url, params=params, headers=headers)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client_factory(
                verify=self._ssl_context,
                trust_env=False,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, url, **kwargs)
        except httpx.ConnectTimeout:
            raise MacroProviderError(
                MacroProviderFailureCategory.TIMEOUT,
                reason_code="CONNECT_TIMEOUT",
                failure_stage="connection",
                request_sent=False,
            ) from None
        except httpx.PoolTimeout:
            raise MacroProviderError(
                MacroProviderFailureCategory.TIMEOUT,
                reason_code="POOL_TIMEOUT",
                failure_stage="connection_pool",
                request_sent=False,
            ) from None
        except httpx.ReadTimeout:
            raise MacroProviderError(
                MacroProviderFailureCategory.TIMEOUT,
                reason_code="READ_TIMEOUT",
                failure_stage="response_wait",
                request_sent=True,
            ) from None
        except httpx.WriteTimeout:
            raise MacroProviderError(
                MacroProviderFailureCategory.TIMEOUT,
                reason_code="WRITE_TIMEOUT",
                failure_stage="request_transmission",
                request_sent=True,
            ) from None
        except httpx.TimeoutException:
            raise MacroProviderError(
                MacroProviderFailureCategory.TIMEOUT,
                reason_code="REQUEST_TIMEOUT",
                failure_stage="request",
                request_sent=False,
            ) from None
        except (httpx.ConnectError, httpx.NetworkError):
            raise MacroProviderError(
                MacroProviderFailureCategory.NETWORK,
                reason_code="NETWORK_FAILURE",
                failure_stage="request",
                request_sent=False,
            ) from None
        except httpx.HTTPError:
            raise MacroProviderError(
                MacroProviderFailureCategory.PROVIDER_ERROR,
                reason_code="HTTP_CLIENT_FAILURE",
                failure_stage="request",
                request_sent=False,
            ) from None

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise MacroProviderError(
                MacroProviderFailureCategory.MALFORMED_RESPONSE,
                reason_code="RESPONSE_TOO_LARGE",
                failure_stage="response_validation",
                request_sent=True,
                http_status_code=response.status_code,
            )
        if response.status_code == 429:
            raise MacroProviderError(
                MacroProviderFailureCategory.RATE_LIMIT,
                reason_code="HTTP_RATE_LIMIT",
                failure_stage="provider_response",
                request_sent=True,
                http_status_code=429,
                retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code in {401, 403}:
            raise MacroProviderError(
                MacroProviderFailureCategory.AUTHENTICATION,
                reason_code="AUTHENTICATION_REJECTED",
                failure_stage="provider_response",
                request_sent=True,
                http_status_code=response.status_code,
            )
        if 400 <= response.status_code < 500:
            raise MacroProviderError(
                MacroProviderFailureCategory.INVALID_REQUEST,
                reason_code="HTTP_INVALID_REQUEST",
                failure_stage="provider_response",
                request_sent=True,
                http_status_code=response.status_code,
            )
        if response.status_code >= 500:
            raise MacroProviderError(
                MacroProviderFailureCategory.SERVER_ERROR,
                reason_code="HTTP_SERVER_ERROR",
                failure_stage="provider_response",
                request_sent=True,
                http_status_code=response.status_code,
            )
        return response


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.isdigit():
        return min(int(normalized), 86_400)
    try:
        parsed = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    delay = ceil((parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    return min(max(delay, 0), 86_400)
