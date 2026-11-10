"""Alpha Vantage client and adapter for provider-neutral news events."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.config import get_alpha_vantage_api_key
from app.models.events import (
    DataDomain,
    EventType,
    NormalizedNewsResearchEvent,
    SourceMetadata,
    SourceTransport,
)

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co"
ALPHA_VANTAGE_NEWS_SOURCE = SourceMetadata(
    source_id="alpha_vantage_news",
    display_name="Alpha Vantage News & Sentiment",
    transport=SourceTransport.API,
    domain=DataDomain.FINANCIAL_NEWS,
)

AlphaVantageNewsSort = Literal["LATEST", "EARLIEST", "RELEVANCE"]

_ALLOWED_SORTS = {"LATEST", "EARLIEST", "RELEVANCE"}
ALPHA_VANTAGE_NEWS_PAGE_SIZE = 50
_ALPHA_VANTAGE_SYMBOL_ALIASES = {
    "FOREX:USD": "USD",
    "FOREX:XAU": "XAU",
}
_NEWS_TIMESTAMP_PATTERN = re.compile(r"^\d{8}T\d{6}$")
_PROVIDER_DIAGNOSTIC_FIELDS = ("Error Message", "message", "Note", "Information")
_PROVIDER_DIAGNOSTIC_MAX_LENGTH = 500
_PROVIDER_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_PARAMETER_PATTERN = re.compile(
    r"\b(api_?key|token|secret|authorization|password|credential)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s&,;]+",
    re.IGNORECASE,
)
_SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "token",
}
_SAFE_REASON_CODE_PATTERN = re.compile(r"^[A-Z0-9_]{1,80}$")
_AUTHENTICATION_DIAGNOSTIC_MARKERS = (
    "invalid api key",
    "api key is invalid",
    "invalid credential",
    "authentication failed",
    "not authorized",
    "unauthorized",
)
_INVALID_REQUEST_DIAGNOSTIC_MARKERS = (
    "invalid api call",
    "invalid request",
    "invalid parameter",
    "invalid news filter",
    "news filters are invalid",
    "missing required",
)
_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


class AlphaVantageError(RuntimeError):
    """Base error for Alpha Vantage news integration failures."""

    default_category = "provider_error"
    default_failure_stage = "unknown"

    def __init__(
        self,
        message: str,
        *,
        category: "AlphaVantageErrorCategory | None" = None,
        http_status_code: int | None = None,
        provider_reason_code: str | None = None,
        failure_stage: str | None = None,
        request_sent: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category or AlphaVantageErrorCategory(self.default_category)
        self.http_status_code = http_status_code
        self.http_status_class = (
            f"{http_status_code // 100}xx" if http_status_code is not None else None
        )
        self.provider_reason_code = _bounded_reason_code(provider_reason_code)
        self.failure_stage = failure_stage or self.default_failure_stage
        self.request_sent = request_sent


class AlphaVantageErrorCategory(StrEnum):
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_ERROR = "provider_error"


class AlphaVantageConfigurationError(AlphaVantageError):
    """Raised when Alpha Vantage is not configured safely."""

    default_failure_stage = "configuration"


class AlphaVantageProviderError(AlphaVantageError):
    """Raised when Alpha Vantage rejects or cannot complete a request."""


class AlphaVantageRateLimitError(AlphaVantageProviderError):
    """Raised when Alpha Vantage reports a request limit."""

    default_category = "rate_limit"


class AlphaVantageTimeoutError(AlphaVantageProviderError):
    """Raised when the provider request exceeds its configured timeout."""

    default_category = "timeout"


class AlphaVantageNetworkError(AlphaVantageProviderError):
    """Raised when DNS, connection, or transport failure prevents a response."""

    default_category = "network"


class AlphaVantageAuthenticationError(AlphaVantageProviderError):
    """Raised when the provider rejects authentication."""

    default_category = "authentication"


class AlphaVantageInvalidRequestError(AlphaVantageProviderError):
    """Raised when the provider rejects request parameters or semantics."""

    default_category = "invalid_request"


class AlphaVantageServerError(AlphaVantageProviderError):
    """Raised for provider HTTP 5xx failures."""

    default_category = "server_error"


class AlphaVantagePayloadError(AlphaVantageError):
    """Raised when Alpha Vantage output cannot be normalized."""

    default_category = "malformed_response"
    default_failure_stage = "article_normalization"


class _ApiKeyQueryTransport(httpx.AsyncBaseTransport):
    """Add the documented API-key parameter below HTTPX's logging boundary."""

    def __init__(
        self,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        authenticated_url = request.url.copy_set_param("apikey", self._api_key)
        extensions = dict(request.extensions)
        extensions.pop("trace", None)
        authenticated_request = httpx.Request(
            method=request.method,
            url=authenticated_url,
            headers=request.headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._transport.handle_async_request(authenticated_request)

    async def aclose(self) -> None:
        await self._transport.aclose()


class AlphaVantageNewsClient:
    """Small asynchronous client for the NEWS_SENTIMENT endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ALPHA_VANTAGE_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise AlphaVantageConfigurationError(
                "Alpha Vantage news is not configured."
            )

        self._api_key = api_key.strip()
        self._base_url = _validate_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str = ALPHA_VANTAGE_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        env_file: Path | None = None,
        load_env_file: bool = True,
    ) -> "AlphaVantageNewsClient":
        api_key = get_alpha_vantage_api_key(
            env_file=env_file,
            load_env_file=load_env_file,
        )
        if api_key is None:
            raise AlphaVantageConfigurationError(
                "Alpha Vantage news is not configured."
            )
        return cls(
            api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    async def fetch_news(
        self,
        *,
        tickers: str | Sequence[str] | None = None,
        topics: str | Sequence[str] | None = None,
        time_from: datetime | str | None = None,
        time_to: datetime | str | None = None,
        sort: AlphaVantageNewsSort | str = "LATEST",
    ) -> tuple[Mapping[str, Any], ...]:
        """Return raw article records after safe response-envelope validation."""
        params = _build_query_params(
            tickers=tickers,
            topics=topics,
            time_from=time_from,
            time_to=time_to,
            sort=sort,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=_ApiKeyQueryTransport(
                    self._api_key,
                    self._transport,
                ),
            ) as client:
                response = await client.get("/query", params=params)
        except httpx.TimeoutException as error:
            raise AlphaVantageTimeoutError(
                "Alpha Vantage news request timed out.",
                provider_reason_code="REQUEST_TIMEOUT",
                failure_stage="request",
                request_sent=_request_sent_state(error),
            ) from None
        except httpx.RequestError as error:
            raise AlphaVantageNetworkError(
                "Alpha Vantage news network request failed.",
                provider_reason_code=(
                    "CONNECTION_FAILED"
                    if isinstance(error, httpx.ConnectError)
                    else "NETWORK_FAILURE"
                ),
                failure_stage="request",
                request_sent=_request_sent_state(error),
            ) from None

        if response.status_code == 429:
            raise AlphaVantageRateLimitError(
                "Alpha Vantage news request limit reached.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_RATE_LIMIT",
                failure_stage="http_response",
                request_sent=True,
            )
        if response.status_code in {401, 403}:
            raise AlphaVantageAuthenticationError(
                "Alpha Vantage news authentication was rejected.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_AUTHENTICATION_REJECTED",
                failure_stage="http_response",
                request_sent=True,
            )
        if response.status_code in {400, 404, 405, 409, 422}:
            raise AlphaVantageInvalidRequestError(
                "Alpha Vantage rejected the news request.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_INVALID_REQUEST",
                failure_stage="http_response",
                request_sent=True,
            )
        if 500 <= response.status_code <= 599:
            raise AlphaVantageServerError(
                "Alpha Vantage news service failed.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_SERVER_ERROR",
                failure_stage="http_response",
                request_sent=True,
            )
        if not response.is_success:
            raise AlphaVantageProviderError(
                "Alpha Vantage news request failed.",
                http_status_code=response.status_code,
                provider_reason_code="HTTP_PROVIDER_ERROR",
                failure_stage="http_response",
                request_sent=True,
            )

        try:
            payload = response.json()
        except ValueError:
            raise AlphaVantagePayloadError(
                "Alpha Vantage returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="INVALID_JSON",
                failure_stage="response_decode",
                request_sent=True,
            ) from None

        if not isinstance(payload, Mapping):
            raise AlphaVantagePayloadError(
                "Alpha Vantage returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="INVALID_RESPONSE_ENVELOPE",
                failure_stage="response_validation",
                request_sent=True,
            )

        _raise_for_provider_message(
            payload,
            forbidden_value=self._api_key,
            http_status_code=response.status_code,
        )
        sanitized_payload = _sanitize_value(payload, forbidden_value=self._api_key)
        feed = sanitized_payload.get("feed")
        if not isinstance(feed, list):
            raise AlphaVantagePayloadError(
                "Alpha Vantage returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="MISSING_NEWS_FEED",
                failure_stage="response_validation",
                request_sent=True,
            )
        if not all(isinstance(article, Mapping) for article in feed):
            raise AlphaVantagePayloadError(
                "Alpha Vantage returned malformed news data.",
                http_status_code=response.status_code,
                provider_reason_code="INVALID_NEWS_FEED",
                failure_stage="response_validation",
                request_sent=True,
            )
        return tuple(dict(article) for article in feed)


class AlphaVantageNewsConnector:
    """Normalize one Alpha Vantage article without external side effects."""

    source = ALPHA_VANTAGE_NEWS_SOURCE

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedNewsResearchEvent:
        article = _sanitize_value(dict(payload))
        publisher_url = _validate_publisher_url(
            _required_text(article.get("url"), "url")
        )
        topic_relevance = _normalize_topic_relevance(article.get("topics"))
        symbol_sentiment = _normalize_symbol_sentiment(
            article.get("ticker_sentiment")
        )

        overall_sentiment = _normalize_overall_sentiment(article)
        published_at = _normalize_published_timestamp(
            article.get("time_published")
        )
        try:
            return NormalizedNewsResearchEvent(
                event_id=_event_id_from_url(publisher_url),
                source=self.source,
                event_type=EventType.NEWS_ITEM,
                provider=_required_text(article.get("source"), "source"),
                title=_required_text(article.get("title"), "title"),
                url=publisher_url,
                timestamp=published_at,
                published_at=published_at,
                symbols=[item["symbol"] for item in symbol_sentiment],
                topics=[item["topic"] for item in topic_relevance],
                authors=_normalize_authors(article.get("authors")),
                publisher_domain=_optional_text(article.get("source_domain")),
                category=_optional_text(article.get("category_within_source")),
                image_url=_optional_text(article.get("banner_image")),
                overall_sentiment=overall_sentiment,
                topic_relevance=topic_relevance,
                symbol_sentiment=symbol_sentiment,
                summary=_required_text(article.get("summary"), "summary"),
                content=None,
            )
        except ValidationError:
            raise AlphaVantagePayloadError(
                "Alpha Vantage returned a malformed news article."
            ) from None


alpha_vantage_news_connector = AlphaVantageNewsConnector()


def _build_query_params(
    *,
    tickers: str | Sequence[str] | None,
    topics: str | Sequence[str] | None,
    time_from: datetime | str | None,
    time_to: datetime | str | None,
    sort: AlphaVantageNewsSort | str,
) -> dict[str, str]:
    normalized_sort = str(sort).strip().upper()
    if normalized_sort not in _ALLOWED_SORTS:
        raise ValueError("sort must be LATEST, EARLIEST, or RELEVANCE.")

    params = {
        "function": "NEWS_SENTIMENT",
        "sort": normalized_sort,
        "limit": str(ALPHA_VANTAGE_NEWS_PAGE_SIZE),
    }
    optional_params = {
        "tickers": _normalize_csv_filter(tickers, "tickers"),
        "topics": _normalize_csv_filter(topics, "topics"),
        "time_from": _normalize_query_timestamp(time_from, "time_from"),
        "time_to": _normalize_query_timestamp(time_to, "time_to"),
    }
    params.update(
        {key: value for key, value in optional_params.items() if value is not None}
    )
    return params


def _normalize_csv_filter(
    value: str | Sequence[str] | None,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    raw_values = value.split(",") if isinstance(value, str) else list(value)
    normalized_values: list[str] = []
    for item in raw_values:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} values must be strings.")
        normalized = item.strip()
        if normalized:
            normalized_values.append(normalized)
    if not normalized_values:
        return None
    return ",".join(dict.fromkeys(normalized_values))


def _normalize_query_timestamp(
    value: datetime | str | None,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{field_name} must include a timezone.")
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid.")
    normalized = value.strip()
    try:
        datetime.strptime(normalized, "%Y%m%dT%H%M")
    except ValueError:
        raise ValueError(f"{field_name} must use YYYYMMDDTHHMM format.") from None
    return normalized


def _raise_for_provider_message(
    payload: Mapping[str, Any],
    *,
    forbidden_value: str,
    http_status_code: int,
) -> None:
    provider_messages = [
        payload[field]
        for field in _PROVIDER_DIAGNOSTIC_FIELDS
        if field in payload
    ]
    if not provider_messages:
        return

    diagnostic = next(
        (
            sanitized
            for message in provider_messages
            if (sanitized := _sanitize_provider_diagnostic(message, forbidden_value))
        ),
        None,
    )
    is_rate_limited = any(
        _is_rate_limit_diagnostic(message) for message in provider_messages
    )
    if is_rate_limited:
        raise AlphaVantageRateLimitError(
            _provider_error_message(
                "Alpha Vantage news request limit reached.",
                diagnostic,
            ),
            http_status_code=http_status_code,
            provider_reason_code="PROVIDER_RATE_LIMIT",
            failure_stage="provider_message",
            request_sent=True,
        )
    category, reason_code, error_type = _classify_provider_diagnostic(
        provider_messages
    )
    raise error_type(
        _provider_error_message(
            "Alpha Vantage news request failed.",
            diagnostic,
        ),
        category=category,
        http_status_code=http_status_code,
        provider_reason_code=reason_code,
        failure_stage="provider_message",
        request_sent=True,
    )


def _classify_provider_diagnostic(
    values: Sequence[Any],
) -> tuple[
    AlphaVantageErrorCategory,
    str,
    type[AlphaVantageProviderError],
]:
    normalized = " ".join(value.lower() for value in values if isinstance(value, str))
    if any(marker in normalized for marker in _AUTHENTICATION_DIAGNOSTIC_MARKERS):
        return (
            AlphaVantageErrorCategory.AUTHENTICATION,
            "PROVIDER_AUTHENTICATION_REJECTED",
            AlphaVantageAuthenticationError,
        )
    if any(marker in normalized for marker in _INVALID_REQUEST_DIAGNOSTIC_MARKERS):
        return (
            AlphaVantageErrorCategory.INVALID_REQUEST,
            "PROVIDER_INVALID_REQUEST",
            AlphaVantageInvalidRequestError,
        )
    return (
        AlphaVantageErrorCategory.PROVIDER_ERROR,
        "PROVIDER_MESSAGE_UNCLASSIFIED",
        AlphaVantageProviderError,
    )


def _sanitize_provider_diagnostic(value: Any, forbidden_value: str) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = " ".join(value.split())
    if not normalized:
        return None

    forbidden_variants = {
        forbidden_value,
        quote(forbidden_value, safe=""),
        quote_plus(forbidden_value, safe=""),
    }
    for forbidden_variant in forbidden_variants:
        if forbidden_variant:
            normalized = normalized.replace(forbidden_variant, "[REDACTED]")
    normalized = _PROVIDER_URL_PATTERN.sub("[REDACTED_URL]", normalized)
    normalized = _SENSITIVE_PARAMETER_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        normalized,
    )
    return normalized[:_PROVIDER_DIAGNOSTIC_MAX_LENGTH]


def _is_rate_limit_diagnostic(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower()
    return any(
        marker in normalized
        for marker in ("rate limit", "call frequency", "requests per")
    )


def _provider_error_message(fallback: str, diagnostic: str | None) -> str:
    if diagnostic is None:
        return fallback
    return f"{fallback} Provider diagnostic: {diagnostic}"


def _bounded_reason_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not _SAFE_REASON_CODE_PATTERN.fullmatch(normalized):
        return "UNCLASSIFIED_PROVIDER_FAILURE"
    return normalized


def _request_sent_state(error: httpx.RequestError) -> bool | None:
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return False
    if isinstance(error, (httpx.ReadError, httpx.ReadTimeout)):
        return True
    return None


def _required_text(value: Any, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise AlphaVantagePayloadError(f"{field_name} is required.")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AlphaVantagePayloadError(
            "Alpha Vantage returned a malformed news article."
        )
    normalized = value.strip()
    return normalized or None


def _normalize_authors(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AlphaVantagePayloadError(
            "Alpha Vantage returned a malformed news article."
        )
    return value


def _normalize_topic_relevance(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    records = _mapping_list(value)
    return [
        {
            "topic": _required_text(record.get("topic"), "topic"),
            "relevance_score": record.get("relevance_score"),
        }
        for record in records
    ]


def _normalize_symbol_sentiment(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    records = _mapping_list(value)
    return [
        {
            "symbol": _normalize_provider_symbol(record.get("ticker")),
            "relevance_score": record.get("relevance_score"),
            "sentiment_score": record.get("ticker_sentiment_score"),
            "sentiment_label": _required_text(
                record.get("ticker_sentiment_label"),
                "ticker_sentiment_label",
            ),
        }
        for record in records
    ]


def _normalize_provider_symbol(value: Any) -> str:
    symbol = _required_text(value, "ticker").upper()
    return _ALPHA_VANTAGE_SYMBOL_ALIASES.get(symbol, symbol)


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise AlphaVantagePayloadError(
            "Alpha Vantage returned a malformed news article."
        )
    return value


def _normalize_overall_sentiment(article: Mapping[str, Any]) -> dict[str, Any] | None:
    score = article.get("overall_sentiment_score")
    label = article.get("overall_sentiment_label")
    if score in (None, "") and label in (None, ""):
        return None
    return {
        "score": score,
        "label": _required_text(label, "overall_sentiment_label"),
    }


def _normalize_published_timestamp(value: Any) -> datetime:
    text = _required_text(value, "time_published")
    if _NEWS_TIMESTAMP_PATTERN.fullmatch(text) is None:
        raise AlphaVantagePayloadError("time_published is invalid.")
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        raise AlphaVantagePayloadError("time_published is invalid.") from None


def _validate_base_url(value: str) -> str:
    try:
        parsed = httpx.URL(value)
    except (TypeError, ValueError):
        raise AlphaVantageConfigurationError(
            "Alpha Vantage base URL is not permitted."
        ) from None

    if (
        parsed.scheme != "https"
        or parsed.host != "www.alphavantage.co"
        or parsed.port not in (None, 443)
        or bool(parsed.username)
        or bool(parsed.password)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise AlphaVantageConfigurationError(
            "Alpha Vantage base URL is not permitted."
        )
    return ALPHA_VANTAGE_BASE_URL


def _validate_publisher_url(value: str) -> str:
    try:
        _HTTP_URL_ADAPTER.validate_python(value)
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
    except (TypeError, ValueError, ValidationError):
        raise AlphaVantagePayloadError("url is invalid.") from None
    return value


def _event_id_from_url(publisher_url: str) -> str:
    identity_url = publisher_url.partition("#")[0]
    digest = sha256(identity_url.encode("utf-8")).hexdigest()
    return f"alpha-vantage-news-{digest}"


def _sanitize_value(value: Any, forbidden_value: str | None = None) -> Any:
    """Remove credential-like fields and redact an echoed credential."""
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(item, forbidden_value)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_FIELD_NAMES
        }
    if isinstance(value, list):
        return [_sanitize_value(item, forbidden_value) for item in value]
    if forbidden_value and isinstance(value, str) and forbidden_value in value:
        return value.replace(forbidden_value, "[REDACTED]")
    return value
