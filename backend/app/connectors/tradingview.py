"""TradingView webhook adapter for the provider-neutral ingestion pipeline."""

import hmac
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status

from app.ingestion.contracts import SourceConnector
from app.models.events import (
    EventType,
    NormalizedDataEvent,
    SourceMetadata,
    SourceTransport,
    DataDomain,
)

WEBHOOK_SECRET_ENV = "TRADINGVIEW_WEBHOOK_SECRET"
TRADINGVIEW_SOURCE = SourceMetadata(
    source_id="tradingview",
    display_name="TradingView",
    transport=SourceTransport.WEBHOOK,
    domain=DataDomain.MARKET_DATA,
)
_PLACEHOLDER_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")
_TEMPLATE_ALIASES = {
    "time": ("timestamp", "timenow"),
    "ticker": ("symbol",),
    "close": ("price",),
}


class TradingViewPayloadError(ValueError):
    """Raised when an alert cannot be converted into an internal market event."""


class TradingViewConnector:
    """Normalizes TradingView alert bodies without invoking FRIDAY Core."""

    source = TRADINGVIEW_SOURCE

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedDataEvent:
        raw_payload = dict(payload)
        raw_symbol = _extract_value(raw_payload, "symbol", "ticker")
        if raw_symbol is None:
            raise TradingViewPayloadError("A resolved symbol or ticker is required.")

        exchange = _optional_text(_extract_value(raw_payload, "exchange"), "exchange")
        symbol = _required_text(raw_symbol, "symbol")
        exchange, symbol = _split_symbol(exchange, symbol)
        alert_name = _optional_text(_extract_value(raw_payload, "alert_name"), "alert_name")

        return NormalizedDataEvent(
            event_id=f"tv-{uuid4().hex}",
            source=self.source,
            event_type=EventType.MARKET_ALERT,
            event=_optional_text(
                _extract_value(raw_payload, "event", "action", "alert_name"),
                "event",
            )
            or "alert",
            symbol=symbol,
            exchange=exchange,
            timeframe=_optional_text(
                _extract_value(raw_payload, "timeframe", "interval"),
                "timeframe",
            ),
            alert_name=alert_name,
            message=_optional_text(_extract_value(raw_payload, "message"), "message"),
            price=_optional_number(_extract_value(raw_payload, "price", "close"), "price"),
            open=_optional_number(_extract_value(raw_payload, "open"), "open"),
            high=_optional_number(_extract_value(raw_payload, "high"), "high"),
            low=_optional_number(_extract_value(raw_payload, "low"), "low"),
            close=_optional_number(_extract_value(raw_payload, "close", "price"), "close"),
            volume=_optional_number(_extract_value(raw_payload, "volume"), "volume"),
            indicator_values=_normalize_indicator_values(raw_payload),
            timestamp=_normalize_timestamp(
                _extract_value(raw_payload, "timestamp", "time", "timenow")
            ),
            raw_payload=raw_payload,
        )


tradingview_connector = TradingViewConnector()


def require_tradingview_token(provided_token: str) -> None:
    """Validate a URL token without exposing or logging secret values."""
    expected_secret = os.getenv(WEBHOOK_SECRET_ENV)
    if expected_secret is None or not expected_secret.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TradingView webhook authentication is not configured.",
        )

    try:
        token_matches = hmac.compare_digest(provided_token, expected_secret)
    except TypeError:
        token_matches = False

    if not token_matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook authentication.",
        )


def normalize_tradingview_payload(payload: Mapping[str, Any]) -> NormalizedDataEvent:
    """Compatibility helper backed by the TradingView source connector."""
    return tradingview_connector.normalize(payload)


def _extract_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in payload:
            continue
        resolved = _resolve_placeholders(payload[key], payload)
        if resolved is not None:
            return resolved
    return None


def _resolve_placeholders(value: Any, payload: dict[str, Any]) -> Any:
    if not isinstance(value, str) or "{{" not in value:
        return value

    def replace(match: re.Match[str]) -> str | None:
        token = match.group(1).strip()
        replacement = payload.get(token)
        if replacement is None:
            for alias in _TEMPLATE_ALIASES.get(token, ()):
                replacement = payload.get(alias)
                if replacement is not None:
                    break
        if replacement is None or replacement == value:
            return None
        resolved_replacement = _resolve_placeholders(replacement, payload)
        if resolved_replacement is None:
            return None
        return str(resolved_replacement)

    result = value
    for match in _PLACEHOLDER_PATTERN.finditer(value):
        replacement = replace(match)
        if replacement is None:
            return None
        result = result.replace(match.group(0), replacement)
    return result


def _required_text(value: Any, field_name: str) -> str:
    normalized = _optional_text(value, field_name)
    if normalized is None:
        raise TradingViewPayloadError(f"{field_name} is required.")
    return normalized


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TradingViewPayloadError(f"{field_name} must be text.")
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.upper() if field_name in {"exchange", "symbol"} else normalized


def _split_symbol(exchange: str | None, symbol: str) -> tuple[str | None, str]:
    if ":" not in symbol:
        return exchange, symbol

    symbol_exchange, symbol_name = symbol.split(":", maxsplit=1)
    if not symbol_exchange or not symbol_name:
        raise TradingViewPayloadError("symbol must use EXCHANGE:SYMBOL when it contains a colon.")
    return exchange or symbol_exchange.upper(), symbol_name.upper()


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TradingViewPayloadError(f"{field_name} must be numeric.")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise TradingViewPayloadError(f"{field_name} must be numeric.") from exc
    if not math.isfinite(normalized):
        raise TradingViewPayloadError(f"{field_name} must be finite.")
    return normalized


def _normalize_indicator_values(payload: dict[str, Any]) -> dict[str, float]:
    """Normalize generic indicator or plot maps without coupling to indicator names."""
    indicator_values: dict[str, float] = {}

    for field_name in ("indicators", "plots", "indicator_values"):
        values = payload.get(field_name)
        if values is None:
            continue
        if not isinstance(values, Mapping):
            raise TradingViewPayloadError(f"{field_name} must be an object of numeric values.")

        for raw_name, raw_value in values.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise TradingViewPayloadError(f"{field_name} keys must be non-empty text.")

            name = raw_name.strip()
            if name in indicator_values:
                raise TradingViewPayloadError("indicator and plot keys must be unique.")

            value = _optional_number(
                _resolve_placeholders(raw_value, payload),
                f"{field_name}.{name}",
            )
            if value is not None:
                indicator_values[name] = value

    return indicator_values


def _normalize_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        return datetime.now(UTC)
    if isinstance(value, bool):
        raise TradingViewPayloadError("timestamp must be an ISO-8601 string or Unix timestamp.")
    if isinstance(value, (int, float)):
        return _timestamp_from_epoch(float(value))
    if not isinstance(value, str):
        raise TradingViewPayloadError("timestamp must be an ISO-8601 string or Unix timestamp.")

    text = value.strip()
    try:
        numeric_value = float(text)
    except ValueError:
        numeric_value = None
    if numeric_value is not None:
        return _timestamp_from_epoch(numeric_value)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradingViewPayloadError("timestamp must be an ISO-8601 string or Unix timestamp.") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _timestamp_from_epoch(value: float) -> datetime:
    if not math.isfinite(value):
        raise TradingViewPayloadError("timestamp must be finite.")
    seconds = value / 1000 if value > 100_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise TradingViewPayloadError("timestamp is out of range.") from exc
