"""Credential-free GDELT DOC 2.0 article-list integration."""

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from hashlib import sha256
from math import ceil
import re
import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.models.events import (
    DataDomain,
    EventType,
    NormalizedNewsResearchEvent,
    SourceMetadata,
    SourceTransport,
)

GDELT_BASE_URL = "https://api.gdeltproject.org"
GDELT_DOC_PATH = "/api/v2/doc/doc"
GDELT_MAX_RECORDS = 50
GDELT_NEWS_SOURCE = SourceMetadata(
    source_id="gdelt_doc_2",
    display_name="GDELT DOC 2.0",
    transport=SourceTransport.API,
    domain=DataDomain.FINANCIAL_NEWS,
)

_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_SEEN_TIMESTAMP = re.compile(r"^\d{8}T\d{6}Z$")
_MAX_RETRY_AFTER_SECONDS = 2_147_483_647


class GdeltErrorCategory(StrEnum):
    NETWORK = "network"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_ERROR = "provider_error"


class GdeltError(RuntimeError):
    default_category = GdeltErrorCategory.PROVIDER_ERROR
    default_stage = "unknown"

    def __init__(
        self,
        message: str,
        *,
        category: GdeltErrorCategory | None = None,
        http_status_code: int | None = None,
        provider_reason_code: str | None = None,
        failure_stage: str | None = None,
        request_sent: bool | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category or self.default_category
        self.http_status_code = http_status_code
        self.http_status_class = (
            f"{http_status_code // 100}xx" if http_status_code is not None else None
        )
        self.provider_reason_code = provider_reason_code
        self.failure_stage = failure_stage or self.default_stage
        self.request_sent = request_sent
        self.retry_after_seconds = retry_after_seconds


class GdeltNetworkError(GdeltError):
    default_category = GdeltErrorCategory.NETWORK


class GdeltInvalidRequestError(GdeltError):
    default_category = GdeltErrorCategory.INVALID_REQUEST


class GdeltRateLimitError(GdeltError):
    default_category = GdeltErrorCategory.RATE_LIMIT


class GdeltTimeoutError(GdeltError):
    default_category = GdeltErrorCategory.TIMEOUT


class GdeltServerError(GdeltError):
    default_category = GdeltErrorCategory.SERVER_ERROR


class GdeltPayloadError(GdeltError):
    default_category = GdeltErrorCategory.MALFORMED_RESPONSE
    default_stage = "article_normalization"


class GdeltNewsClient:
    """Fetch one bounded GDELT article-list search without retries."""

    def __init__(
        self,
        *,
        base_url: str = GDELT_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        now=None,
    ) -> None:
        self._base_url = str(_HTTP_URL_ADAPTER.validate_python(base_url)).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._ssl_context = ssl.create_default_context()
        self._now = now or (lambda: datetime.now(UTC))

    async def fetch_articles(self, *, query: str) -> tuple[Mapping[str, Any], ...]:
        normalized_query = " ".join(query.split())
        if not normalized_query or len(normalized_query) > 500:
            raise ValueError("GDELT query must contain 1 to 500 characters.")
        params = {
            "query": normalized_query,
            "mode": "artlist",
            "format": "json",
            "timespan": "1week",
            "maxrecords": str(GDELT_MAX_RECORDS),
            "sort": "datedesc",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
                trust_env=False,
                verify=self._ssl_context,
            ) as client:
                response = await client.get(GDELT_DOC_PATH, params=params)
        except httpx.TimeoutException as error:
            raise GdeltTimeoutError(
                "GDELT news request timed out.",
                provider_reason_code=_timeout_reason_code(error),
                failure_stage="request",
                request_sent=_request_sent_state(error),
            ) from None
        except httpx.RequestError as error:
            raise GdeltNetworkError(
                "GDELT news network request failed.",
                provider_reason_code=(
                    "CONNECTION_FAILED"
                    if isinstance(error, httpx.ConnectError)
                    else "NETWORK_FAILURE"
                ),
                failure_stage="request",
                request_sent=_request_sent_state(error),
            ) from None

        if response.status_code == 429:
            raise GdeltRateLimitError(
                "GDELT request limit reached.",
                http_status_code=429,
                provider_reason_code="HTTP_RATE_LIMIT",
                failure_stage="provider_response",
                request_sent=True,
                retry_after_seconds=_retry_after_seconds(
                    response.headers.get("Retry-After"),
                    now=self._now(),
                ),
            )
        if 400 <= response.status_code <= 499:
            raise GdeltInvalidRequestError(
                "GDELT rejected the news request.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_INVALID_REQUEST",
                failure_stage="http_response",
                request_sent=True,
            )
        if 500 <= response.status_code <= 599:
            raise GdeltServerError(
                "GDELT news service failed.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_SERVER_ERROR",
                failure_stage="http_response",
                request_sent=True,
            )
        if not response.is_success:
            raise GdeltError(
                "GDELT news request failed.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_PROVIDER_ERROR",
                failure_stage="http_response",
                request_sent=True,
            )
        try:
            payload = response.json()
        except ValueError:
            raise GdeltPayloadError(
                "GDELT returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="INVALID_JSON",
                failure_stage="response_decode",
                request_sent=True,
            ) from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("articles"), list):
            raise GdeltPayloadError(
                "GDELT returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="INVALID_ARTICLE_LIST",
                failure_stage="response_validation",
                request_sent=True,
            )
        articles = payload["articles"]
        if not all(isinstance(article, Mapping) for article in articles):
            raise GdeltPayloadError(
                "GDELT returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="INVALID_ARTICLE_RECORD",
                failure_stage="response_validation",
                request_sent=True,
            )
        return tuple(dict(article) for article in articles)


class GdeltNewsConnector:
    """Normalize GDELT article metadata without treating it as article content."""

    source = GDELT_NEWS_SOURCE

    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        search_query: str,
        relevance_category: str,
    ) -> NormalizedNewsResearchEvent:
        title = _required_text(payload.get("title"), "title")
        article_url = _validate_url(_required_text(payload.get("url"), "url"))
        seen_at = _seen_timestamp(payload.get("seendate"))
        publisher_domain = _optional_text(payload.get("domain")) or urlsplit(
            article_url
        ).hostname
        try:
            return NormalizedNewsResearchEvent(
                event_id=sha256(article_url.encode("utf-8")).hexdigest(),
                source=self.source,
                event_type=EventType.NEWS_ITEM,
                provider="GDELT DOC 2.0",
                title=title,
                url=article_url,
                timestamp=seen_at,
                published_at=None,
                seen_at=seen_at,
                publisher_domain=publisher_domain,
                language=_optional_text(payload.get("language")),
                source_country=_optional_text(payload.get("sourcecountry")),
                search_query=search_query,
                search_category=relevance_category,
                summary=None,
                content=None,
            )
        except ValidationError:
            raise GdeltPayloadError(
                "GDELT returned a malformed news article."
            ) from None


gdelt_news_connector = GdeltNewsConnector()


def _required_text(value: Any, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise GdeltPayloadError(f"GDELT article {field_name} is required.")
    return normalized


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _validate_url(value: str) -> str:
    try:
        parsed = _HTTP_URL_ADAPTER.validate_python(value)
    except ValidationError:
        raise GdeltPayloadError("GDELT article URL is invalid.") from None
    if parsed.scheme not in {"http", "https"}:
        raise GdeltPayloadError("GDELT article URL is invalid.")
    return str(parsed)


def _seen_timestamp(value: Any) -> datetime:
    text = _required_text(value, "seendate")
    if not _SEEN_TIMESTAMP.fullmatch(text):
        raise GdeltPayloadError("GDELT article seen timestamp is invalid.")
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise GdeltPayloadError("GDELT article seen timestamp is invalid.") from None


def _request_sent_state(error: httpx.RequestError) -> bool | None:
    # This records HTTP request transmission, not whether DNS, TCP, or TLS work began.
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return False
    if isinstance(error, (httpx.ReadError, httpx.ReadTimeout)):
        return True
    return None


def _timeout_reason_code(error: httpx.TimeoutException) -> str:
    if isinstance(error, httpx.ConnectTimeout):
        return "CONNECT_TIMEOUT"
    if isinstance(error, httpx.ReadTimeout):
        return "READ_TIMEOUT"
    if isinstance(error, httpx.WriteTimeout):
        return "WRITE_TIMEOUT"
    if isinstance(error, httpx.PoolTimeout):
        return "POOL_TIMEOUT"
    return "REQUEST_TIMEOUT"


def _retry_after_seconds(value: str | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return None
    if normalized.isascii() and normalized.isdigit():
        seconds = int(normalized)
        return seconds if seconds <= _MAX_RETRY_AFTER_SECONDS else None
    try:
        retry_at = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        return None
    reference = now.astimezone(UTC)
    delay = max(0, ceil((retry_at.astimezone(UTC) - reference).total_seconds()))
    return delay if delay <= _MAX_RETRY_AFTER_SECONDS else None
