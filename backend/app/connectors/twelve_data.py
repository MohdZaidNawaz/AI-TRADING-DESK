"""Twelve Data client and adapter for provider-neutral market events."""

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.config import get_twelve_data_api_key
from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    SourceMetadata,
    SourceTransport,
)

TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
TWELVE_DATA_SOURCE = SourceMetadata(
    source_id="twelve_data",
    display_name="Twelve Data",
    transport=SourceTransport.API,
    domain=DataDomain.MARKET_DATA,
)

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9._/-]+$")
_SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "token",
}


class TwelveDataError(RuntimeError):
    """Base error for Twelve Data integration failures."""


class TwelveDataConfigurationError(TwelveDataError):
    """Raised when the connector cannot be configured safely."""


class TwelveDataProviderError(TwelveDataError):
    """Raised when Twelve Data rejects or cannot complete a request."""


class TwelveDataPayloadError(TwelveDataError):
    """Raised when provider output cannot be normalized."""


class TwelveDataClient:
    """Small asynchronous client for the Twelve Data quote endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TWELVE_DATA_BASE_URL,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise TwelveDataConfigurationError("Twelve Data is not configured.")

        self._api_key = api_key.strip()
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str = TWELVE_DATA_BASE_URL,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        env_file: Path | None = None,
        load_env_file: bool = True,
    ) -> "TwelveDataClient":
        api_key = get_twelve_data_api_key(
            env_file=env_file,
            load_env_file=load_env_file,
        )
        if api_key is None:
            raise TwelveDataConfigurationError("Twelve Data is not configured.")
        return cls(
            api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    async def fetch_snapshot(self, symbol: str) -> Mapping[str, Any]:
        normalized_symbol = _normalize_symbol(symbol)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    "/quote",
                    params={
                        "symbol": normalized_symbol,
                        "interval": "1min",
                        "timezone": "UTC",
                    },
                    headers={"Authorization": f"apikey {self._api_key}"},
                )
        except httpx.RequestError:
            raise TwelveDataProviderError("Twelve Data request failed.") from None

        if not response.is_success:
            raise TwelveDataProviderError("Twelve Data request failed.")

        try:
            payload = response.json()
        except ValueError:
            raise TwelveDataPayloadError("Twelve Data returned malformed market data.") from None

        if not isinstance(payload, dict):
            raise TwelveDataPayloadError("Twelve Data returned malformed market data.")
        if payload.get("status") == "error":
            raise TwelveDataProviderError("Twelve Data request failed.")
        return _sanitize_value(payload, forbidden_value=self._api_key)


class TwelveDataConnector:
    """Normalize Twelve Data quote output without invoking FRIDAY Core."""

    source = TWELVE_DATA_SOURCE

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedDataEvent:
        raw_payload = _sanitize_value(dict(payload))
        symbol = _normalize_symbol(_required_text(raw_payload.get("symbol"), "symbol"))
        price = _required_number(
            _first_present(raw_payload, "price", "close", "rate"),
            "price",
        )

        return NormalizedDataEvent(
            event_id=f"td-{uuid4().hex}",
            source=self.source,
            event_type=EventType.MARKET_QUOTE,
            event="market_snapshot",
            asset_class=_infer_asset_class(symbol, raw_payload),
            symbol=symbol,
            exchange=_optional_text(raw_payload.get("exchange")),
            timeframe=_optional_text(raw_payload.get("interval")) or "1min",
            price=price,
            open=_optional_number(raw_payload.get("open"), "open"),
            high=_optional_number(raw_payload.get("high"), "high"),
            low=_optional_number(raw_payload.get("low"), "low"),
            close=_optional_number(raw_payload.get("close"), "close")
            if raw_payload.get("close") not in (None, "")
            else price,
            volume=_optional_number(raw_payload.get("volume"), "volume"),
            timestamp=_normalize_timestamp(
                _first_present(raw_payload, "last_update_at", "timestamp", "datetime")
            ),
            attributes=_normalize_attributes(raw_payload),
            raw_payload=raw_payload,
        )


twelve_data_connector = TwelveDataConnector()


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol or len(symbol) > 40 or _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise TwelveDataPayloadError("symbol is invalid.")
    return symbol


def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def _required_text(value: Any, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise TwelveDataPayloadError(f"{field_name} is required.")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise TwelveDataPayloadError("Twelve Data returned malformed market data.")
    normalized = str(value).strip()
    return normalized or None


def _required_number(value: Any, field_name: str) -> float:
    normalized = _optional_number(value, field_name)
    if normalized is None:
        raise TwelveDataPayloadError(f"{field_name} is required.")
    return normalized


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TwelveDataPayloadError(f"{field_name} must be numeric.")
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise TwelveDataPayloadError(f"{field_name} must be numeric.") from None
    if not math.isfinite(normalized):
        raise TwelveDataPayloadError(f"{field_name} must be finite.")
    return normalized


def _normalize_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        return datetime.now(UTC)
    if isinstance(value, bool):
        raise TwelveDataPayloadError("timestamp is invalid.")
    if isinstance(value, (int, float)):
        return _timestamp_from_epoch(float(value))
    if not isinstance(value, str):
        raise TwelveDataPayloadError("timestamp is invalid.")

    text = value.strip()
    try:
        numeric_value = float(text)
    except ValueError:
        numeric_value = None
    if numeric_value is not None:
        return _timestamp_from_epoch(numeric_value)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise TwelveDataPayloadError("timestamp is invalid.") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _timestamp_from_epoch(value: float) -> datetime:
    if not math.isfinite(value):
        raise TwelveDataPayloadError("timestamp is invalid.")
    seconds = value / 1000 if value > 100_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise TwelveDataPayloadError("timestamp is invalid.") from None


def _infer_asset_class(symbol: str, payload: Mapping[str, Any]) -> AssetClass:
    base_symbol = symbol.split("/", maxsplit=1)[0]
    provider_type = " ".join(
        str(payload.get(key, ""))
        for key in ("type", "instrument_type", "exchange")
    ).lower()

    if base_symbol in {"XAU", "XAG", "XPT", "XPD"} or "commodity" in provider_type:
        return AssetClass.COMMODITY
    if "crypto" in provider_type or "digital currency" in provider_type:
        return AssetClass.CRYPTO
    if "/" in symbol or "forex" in provider_type:
        return AssetClass.FOREX
    if "index" in provider_type:
        return AssetClass.INDEX
    if "stock" in provider_type or "equity" in provider_type:
        return AssetClass.EQUITY
    return AssetClass.UNKNOWN


def _normalize_attributes(payload: Mapping[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for key in ("name", "currency", "mic_code"):
        value = _optional_text(payload.get(key))
        if value is not None:
            attributes[key] = value

    for key in ("previous_close", "change", "percent_change", "average_volume"):
        value = _optional_number(payload.get(key), key)
        if value is not None:
            attributes[key] = value

    is_market_open = payload.get("is_market_open")
    if isinstance(is_market_open, bool):
        attributes["is_market_open"] = is_market_open
    return attributes


def _sanitize_value(value: Any, forbidden_value: str | None = None) -> Any:
    """Remove credential-like fields and an echoed credential from provider output."""
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(item, forbidden_value)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_FIELD_NAMES
            and not (
                forbidden_value is not None
                and isinstance(item, str)
                and item == forbidden_value
            )
        }
    if isinstance(value, list):
        return [_sanitize_value(item, forbidden_value) for item in value]
    if forbidden_value is not None and isinstance(value, str) and value == forbidden_value:
        return None
    return value
