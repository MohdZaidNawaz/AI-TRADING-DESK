"""Twelve Data client and adapter for provider-neutral market events."""

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from app.models.instruments import (
    ExchangeResolution,
    InstrumentAlias,
    InstrumentCatalogueEntry,
    InstrumentCataloguePage,
    InstrumentCategory,
    InstrumentProviderMapping,
    InstrumentSourceProvenance,
    canonical_instrument_id,
)
from app.models.market_series import (
    MarketBarState,
    NormalizedMarketBar,
    VolumeKind,
    canonical_market_symbol,
    canonical_timeframe,
    timeframe_seconds,
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

TWELVE_DATA_HISTORICAL_INTERVALS = MappingProxyType(
    {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1day",
        "1w": "1week",
    }
)
TWELVE_DATA_MAX_OUTPUT_SIZE = 5_000
TWELVE_DATA_HISTORICAL_REQUEST_TIMEZONE = "UTC"


@dataclass(frozen=True, slots=True)
class TwelveDataHistoricalBatch:
    """Fully validated historical response; safe to ingest atomically as a batch."""

    provider_symbol: str
    provider_interval: str
    requested_timezone: str | None
    source_timezone: str
    venue: str | None
    exchange_metadata_available: bool
    retrieved_at: datetime
    requested_count: int
    bars: tuple[NormalizedMarketBar, ...]
    missing_interval_count: int
    partial: bool
    warning_codes: tuple[str, ...]


class TwelveDataError(RuntimeError):
    """Base error for Twelve Data integration failures."""


class TwelveDataConfigurationError(TwelveDataError):
    """Raised when the connector cannot be configured safely."""


class TwelveDataProviderError(TwelveDataError):
    """Raised when Twelve Data rejects or cannot complete a request."""

    def __init__(
        self,
        message: str = "Twelve Data request failed.",
        *,
        category: str = "provider_error",
        status_code: int | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.provider_code = provider_code


class TwelveDataPayloadError(TwelveDataError):
    """Raised when provider output cannot be normalized."""

    def __init__(
        self,
        message: str = "Twelve Data returned malformed data.",
        *,
        category: str = "malformed_response",
    ) -> None:
        super().__init__(message)
        self.category = category


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
        return await self._fetch_json(
            "/quote",
            {
                "symbol": normalized_symbol,
                "interval": "1min",
                "timezone": "UTC",
            },
            payload_error="Twelve Data returned malformed market data.",
        )

    async def fetch_time_series(
        self,
        symbol: str,
        interval: str,
        *,
        outputsize: int,
        start_date: datetime | None = None,
    ) -> Mapping[str, Any]:
        """Fetch one bounded historical response without retrying provider failures."""
        normalized_symbol = _normalize_symbol(symbol)
        provider_interval = twelve_data_historical_interval(interval)
        requested_outputsize = _bounded_output_size(outputsize)
        if start_date is not None:
            if start_date.tzinfo is None or start_date.utcoffset() is None:
                raise TwelveDataPayloadError("Historical start date is invalid.")
            normalized_start_date = start_date.astimezone(UTC).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
        else:
            normalized_start_date = None
        return await self._fetch_json(
            "/time_series",
            {
                key: value
                for key, value in {
                    "symbol": normalized_symbol,
                    "interval": provider_interval,
                    "outputsize": requested_outputsize,
                    "timezone": TWELVE_DATA_HISTORICAL_REQUEST_TIMEZONE,
                    "order": "ASC",
                    "start_date": normalized_start_date,
                }.items()
                if value is not None
            },
            payload_error="Twelve Data returned malformed historical data.",
        )

    async def fetch_forex_pairs(
        self,
        *,
        symbol: str | None = None,
        currency_base: str | None = None,
        currency_quote: str | None = None,
        page: int = 1,
        outputsize: int | None = None,
    ) -> InstrumentCataloguePage:
        """Return one normalized page from Twelve Data's forex directory."""
        payload = await self._fetch_catalogue(
            "/forex_pairs",
            {
                "symbol": _optional_symbol_filter(symbol),
                "currency_base": _optional_filter_text(currency_base, "currency_base"),
                "currency_quote": _optional_filter_text(currency_quote, "currency_quote"),
                "page": _positive_request_integer(page, "page"),
                "outputsize": _optional_positive_request_integer(outputsize, "outputsize"),
            },
        )
        return _normalize_catalogue_page(
            payload,
            category=InstrumentCategory.FOREX,
            page=page,
            outputsize=outputsize,
        )

    async def fetch_commodities(
        self,
        *,
        symbol: str | None = None,
        category: str | None = None,
        page: int = 1,
        outputsize: int | None = None,
    ) -> InstrumentCataloguePage:
        """Return one normalized page from Twelve Data's commodity directory."""
        payload = await self._fetch_catalogue(
            "/commodities",
            {
                "symbol": _optional_symbol_filter(symbol),
                "category": _optional_filter_text(category, "category"),
                "page": _positive_request_integer(page, "page"),
                "outputsize": _optional_positive_request_integer(outputsize, "outputsize"),
            },
        )
        return _normalize_catalogue_page(
            payload,
            category=InstrumentCategory.COMMODITY,
            page=page,
            outputsize=outputsize,
        )

    async def fetch_cryptocurrencies(
        self,
        *,
        symbol: str | None = None,
        exchange: str | None = None,
        currency_base: str | None = None,
        currency_quote: str | None = None,
        page: int = 1,
        outputsize: int | None = None,
    ) -> InstrumentCataloguePage:
        """Return one normalized page from Twelve Data's crypto directory."""
        payload = await self._fetch_catalogue(
            "/cryptocurrencies",
            {
                "symbol": _optional_symbol_filter(symbol),
                "exchange": _optional_filter_text(exchange, "exchange"),
                "currency_base": _optional_filter_text(currency_base, "currency_base"),
                "currency_quote": _optional_filter_text(currency_quote, "currency_quote"),
                "page": _positive_request_integer(page, "page"),
                "outputsize": _optional_positive_request_integer(outputsize, "outputsize"),
            },
        )
        return _normalize_catalogue_page(
            payload,
            category=InstrumentCategory.CRYPTO,
            page=page,
            outputsize=outputsize,
        )

    async def _fetch_catalogue(
        self,
        endpoint: str,
        params: Mapping[str, str | int | None],
    ) -> Mapping[str, Any]:
        return await self._fetch_json(
            endpoint,
            {key: value for key, value in params.items() if value is not None},
            payload_error="Twelve Data returned malformed catalogue data.",
        )

    async def _fetch_json(
        self,
        endpoint: str,
        params: Mapping[str, str | int],
        *,
        payload_error: str,
    ) -> Mapping[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    endpoint,
                    params=params,
                    headers={"Authorization": f"apikey {self._api_key}"},
                )
        except httpx.TimeoutException:
            raise TwelveDataProviderError(
                "Twelve Data request failed.",
                category="timeout",
            ) from None
        except httpx.RequestError:
            raise TwelveDataProviderError(
                "Twelve Data request failed.",
                category="network",
            ) from None

        if not response.is_success:
            category = "http_error"
            if response.status_code in (401, 403):
                category = "authentication"
            elif response.status_code == 429:
                category = "rate_limit"
            elif response.status_code >= 500:
                category = "server_error"
            raise TwelveDataProviderError(
                "Twelve Data request failed.",
                category=category,
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError:
            raise TwelveDataPayloadError(payload_error, category="malformed_response") from None

        if not isinstance(payload, dict):
            raise TwelveDataPayloadError(payload_error, category="malformed_response")
        if payload.get("status") == "error":
            provider_code = payload.get("code")
            if isinstance(provider_code, (str, int)):
                provider_code = str(provider_code)
            else:
                provider_code = None
            raise TwelveDataProviderError(
                "Twelve Data request failed.",
                category="provider_error",
                status_code=response.status_code,
                provider_code=provider_code,
            )
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


class TwelveDataHistoricalConnector:
    """Validate Twelve Data candles before any result reaches shared evidence."""

    source = TWELVE_DATA_SOURCE

    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        expected_symbol: str,
        timeframe: str,
        requested_count: int,
        retrieved_at: datetime,
        requested_timezone: str | None = None,
    ) -> TwelveDataHistoricalBatch:
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise TwelveDataPayloadError("Historical retrieval time is invalid.")
        retrieved_at = retrieved_at.astimezone(UTC)
        normalized_timeframe = _historical_internal_timeframe(timeframe)
        provider_interval = twelve_data_historical_interval(normalized_timeframe)
        requested_count = _bounded_output_size(requested_count)

        meta = payload.get("meta")
        values = payload.get("values")
        if not isinstance(meta, Mapping) or not isinstance(values, list):
            raise TwelveDataPayloadError("Twelve Data returned malformed historical data.")
        provider_symbol = _normalize_symbol(
            _required_text(meta.get("symbol"), "historical symbol")
        )
        if canonical_market_symbol(provider_symbol) != canonical_market_symbol(expected_symbol):
            raise TwelveDataPayloadError("Twelve Data returned an unexpected historical symbol.")
        response_interval = _required_text(meta.get("interval"), "historical interval")
        if response_interval.casefold() != provider_interval.casefold():
            raise TwelveDataPayloadError("Twelve Data returned an unexpected historical interval.")
        exchange_timezone = _optional_text(meta.get("exchange_timezone"))
        if exchange_timezone is not None:
            _timezone_info(exchange_timezone)
        normalized_requested_timezone = _normalized_requested_timezone(requested_timezone)
        # Twelve Data applies the requested UTC output timezone to intraday
        # values. For daily and weekly bars its documented behavior is to
        # ignore that request and return exchange-local datetimes instead.
        if normalized_timeframe in {"1d", "1w"}:
            if exchange_timezone is None:
                raise TwelveDataPayloadError(
                    "Twelve Data returned incomplete historical timezone metadata."
                )
            source_timezone = exchange_timezone
        elif normalized_requested_timezone is not None:
            if exchange_timezone is None and normalized_requested_timezone != "UTC":
                raise TwelveDataPayloadError(
                    "Twelve Data returned incomplete historical timezone metadata."
                )
            source_timezone = normalized_requested_timezone
        elif exchange_timezone is not None:
            source_timezone = exchange_timezone
        else:
            raise TwelveDataPayloadError(
                "Twelve Data returned incomplete historical timezone metadata."
            )
        if len(values) > requested_count:
            raise TwelveDataPayloadError("Twelve Data returned an oversized historical response.")

        venue = _optional_text(meta.get("exchange"))
        parsed: list[NormalizedMarketBar] = []
        source_opened_at: list[datetime] = []
        seen: set[datetime] = set()
        duration = timedelta(seconds=timeframe_seconds(normalized_timeframe))
        for raw in values:
            if not isinstance(raw, Mapping):
                raise TwelveDataPayloadError("Twelve Data returned malformed historical data.")
            source_timestamp = _required_text(raw.get("datetime"), "historical datetime")
            opened_at = _historical_timestamp(source_timestamp, source_timezone)
            if opened_at in seen:
                raise TwelveDataPayloadError("Twelve Data returned duplicate historical timestamps.")
            seen.add(opened_at)
            source_opened_at.append(opened_at)
            closed_at = opened_at + duration
            volume_value = raw.get("volume")
            volume = (
                None
                if volume_value in (None, "")
                else _required_number(volume_value, "volume")
            )
            try:
                parsed.append(
                    NormalizedMarketBar(
                        source=self.source,
                        symbol=provider_symbol,
                        asset_class=_infer_asset_class(provider_symbol, meta),
                        venue=venue,
                        timeframe=normalized_timeframe,
                        opened_at=opened_at,
                        closed_at=closed_at,
                        open=_required_number(raw.get("open"), "open"),
                        high=_required_number(raw.get("high"), "high"),
                        low=_required_number(raw.get("low"), "low"),
                        close=_required_number(raw.get("close"), "close"),
                        volume=volume,
                        volume_kind=(
                            VolumeKind.UNAVAILABLE if volume is None else VolumeKind.UNKNOWN
                        ),
                        state=(
                            MarketBarState.COMPLETED
                            if closed_at <= retrieved_at
                            else MarketBarState.PROVISIONAL
                        ),
                        received_at=retrieved_at,
                        source_timestamp=source_timestamp,
                    )
                )
            except ValueError:
                raise TwelveDataPayloadError(
                    "Twelve Data returned invalid historical candle values."
                ) from None

        _validate_provider_order(source_opened_at)
        parsed.sort(key=lambda item: item.opened_at)
        missing_interval_count = _missing_interval_count(parsed)
        data_warnings: list[str] = []
        if not parsed:
            data_warnings.append("HISTORICAL_RESPONSE_EMPTY")
        if len(parsed) < requested_count:
            data_warnings.append("HISTORICAL_RESPONSE_SHORT")
        if missing_interval_count:
            data_warnings.append("HISTORICAL_INTERVALS_MISSING")
        if any(bar.state is MarketBarState.PROVISIONAL for bar in parsed):
            data_warnings.append("HISTORICAL_PROVISIONAL_BAR_PRESENT")
        exchange_metadata_available = exchange_timezone is not None and venue is not None
        warning_codes = list(data_warnings)
        if not exchange_metadata_available:
            warning_codes.append("HISTORICAL_EXCHANGE_METADATA_UNAVAILABLE")
        return TwelveDataHistoricalBatch(
            provider_symbol=provider_symbol,
            provider_interval=provider_interval,
            requested_timezone=normalized_requested_timezone,
            source_timezone=source_timezone,
            venue=venue.upper() if venue else None,
            exchange_metadata_available=exchange_metadata_available,
            retrieved_at=retrieved_at,
            requested_count=requested_count,
            bars=tuple(parsed),
            missing_interval_count=missing_interval_count,
            partial=bool(data_warnings),
            warning_codes=tuple(warning_codes),
        )


twelve_data_historical_connector = TwelveDataHistoricalConnector()


def twelve_data_historical_interval(timeframe: str) -> str:
    normalized = _historical_internal_timeframe(timeframe)
    try:
        return TWELVE_DATA_HISTORICAL_INTERVALS[normalized]
    except KeyError:
        raise TwelveDataPayloadError("Historical interval is not supported.") from None


def _historical_internal_timeframe(value: str) -> str:
    aliases = {"1day": "1d", "1week": "1w"}
    normalized = aliases.get(value.strip().lower(), value.strip().lower())
    try:
        normalized = canonical_timeframe(normalized)
    except ValueError:
        raise TwelveDataPayloadError("Historical interval is not supported.") from None
    if normalized not in TWELVE_DATA_HISTORICAL_INTERVALS:
        raise TwelveDataPayloadError("Historical interval is not supported.")
    return normalized


def _bounded_output_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TwelveDataPayloadError("Historical output size is invalid.")
    if value < 1 or value > TWELVE_DATA_MAX_OUTPUT_SIZE:
        raise TwelveDataPayloadError("Historical output size is invalid.")
    return value


def _historical_timestamp(value: str, timezone_name: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise TwelveDataPayloadError("Twelve Data returned an invalid historical timestamp.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_timezone_info(timezone_name))
    return parsed.astimezone(UTC)


def _normalized_requested_timezone(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _required_text(value, "requested historical timezone")
    _timezone_info(normalized)
    return "UTC" if normalized.upper() == "UTC" else normalized


def _timezone_info(value: str) -> tzinfo:
    normalized = value.strip()
    if normalized.upper() in {"UTC", "GMT"}:
        return UTC
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        raise TwelveDataPayloadError(
            "Twelve Data returned an unsupported historical timezone."
        ) from None


def _validate_provider_order(opened_at: list[datetime]) -> None:
    if len(opened_at) < 2:
        return
    ascending = all(left < right for left, right in zip(opened_at, opened_at[1:]))
    descending = all(left > right for left, right in zip(opened_at, opened_at[1:]))
    if not ascending and not descending:
        raise TwelveDataPayloadError("Twelve Data returned out-of-order historical data.")


def _missing_interval_count(bars: list[NormalizedMarketBar]) -> int:
    missing = 0
    for left, right in zip(bars, bars[1:]):
        interval = left.timeframe_seconds
        delta = int((right.opened_at - left.opened_at).total_seconds())
        if delta > interval:
            missing += max(0, delta // interval - 1)
    return missing


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol or len(symbol) > 40 or _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise TwelveDataPayloadError("symbol is invalid.")
    return symbol


def _optional_symbol_filter(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_symbol(value)


def _optional_filter_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TwelveDataPayloadError(f"{field_name} is invalid.")
    normalized = value.strip()
    if not normalized:
        raise TwelveDataPayloadError(f"{field_name} is invalid.")
    return normalized


def _positive_request_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TwelveDataPayloadError(f"{field_name} is invalid.")
    return value


def _optional_positive_request_integer(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_request_integer(value, field_name)


def _normalize_catalogue_page(
    payload: Mapping[str, Any],
    *,
    category: InstrumentCategory,
    page: int,
    outputsize: int | None,
) -> InstrumentCataloguePage:
    raw_count = payload.get("count")
    raw_records = payload.get("data")
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        logger.warning("catalogue normalization failed: category=%s detail=count invalid type=%s", category.value, type(raw_count).__name__)
        raise TwelveDataPayloadError("Twelve Data returned malformed catalogue data.")
    if not isinstance(raw_records, list):
        logger.warning("catalogue normalization failed: category=%s detail=data invalid type=%s", category.value, type(raw_records).__name__)
        raise TwelveDataPayloadError("Twelve Data returned malformed catalogue data.")
    if raw_count > 0 and not raw_records:
        raise TwelveDataPayloadError("Twelve Data returned incomplete catalogue data.")

    observed_at = datetime.now(UTC)
    instruments: list[InstrumentCatalogueEntry] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            raise TwelveDataPayloadError("Twelve Data returned malformed catalogue data.")
        try:
            instruments.append(
                _normalize_catalogue_entry(
                    raw_record,
                    category,
                    observed_at=observed_at,
                )
            )
        except TwelveDataPayloadError as exc:
            logger.warning(
                "catalogue normalization failed: category=%s detail=%s",
                category.value,
                str(exc),
            )
            raise

    return InstrumentCataloguePage(
        source_id=TWELVE_DATA_SOURCE.source_id,
        category=category,
        page=page,
        outputsize=outputsize,
        provider_count=raw_count,
        instruments=tuple(instruments),
    )


def _normalize_catalogue_entry(
    payload: Mapping[str, Any],
    category: InstrumentCategory,
    *,
    observed_at: datetime,
) -> InstrumentCatalogueEntry:
    provider_symbol = _normalize_symbol(
        _required_text(payload.get("symbol"), "catalogue symbol")
    )
    display_name = _optional_text(payload.get("name"))
    base_currency = _optional_text(payload.get("currency_base"))
    quote_currency = _optional_text(payload.get("currency_quote"))
    commodity_category = _optional_text(payload.get("category"))
    forex_group = _optional_text(payload.get("currency_group"))
    description = _optional_text(payload.get("description"))
    available_exchanges = _normalize_exchange_identifiers(
        payload.get("available_exchanges")
    )
    exchange_id = (_optional_text(payload.get("exchange")) or "GLOBAL").upper()
    exchange_resolution = (
        ExchangeResolution.VENUE_NEUTRAL
        if exchange_id == "GLOBAL"
        else ExchangeResolution.RESOLVED
    )
    canonical_symbol = provider_symbol.upper()
    instrument_id = (
        f"{TWELVE_DATA_SOURCE.source_id}:{category.value}:{provider_symbol}"
        if exchange_id == "GLOBAL"
        else (
            f"{TWELVE_DATA_SOURCE.source_id}:{category.value}:"
            f"{exchange_id}:{provider_symbol}"
        )
    )
    display_symbol = provider_symbol.replace("/", "")
    aliases = (
        (
            InstrumentAlias(
                alias=display_symbol,
                exchange_id=exchange_id,
                source_id=TWELVE_DATA_SOURCE.source_id,
            ),
        )
        if display_symbol != provider_symbol
        else ()
    )

    return InstrumentCatalogueEntry(
        instrument_id=instrument_id,
        canonical_instrument_id=canonical_instrument_id(
            category, exchange_id, canonical_symbol
        ),
        source_id=TWELVE_DATA_SOURCE.source_id,
        category=category,
        provider_symbol=provider_symbol,
        canonical_symbol=canonical_symbol,
        exchange_id=exchange_id,
        exchange_resolution=exchange_resolution,
        exchange_name=_optional_text(payload.get("exchange")),
        display_symbol=display_symbol,
        display_name=display_name,
        base_currency=base_currency,
        quote_currency=quote_currency,
        forex_group=forex_group,
        commodity_category=commodity_category,
        description=description,
        available_exchanges=available_exchanges,
        provider_mappings=(
            InstrumentProviderMapping(
                source_id=TWELVE_DATA_SOURCE.source_id,
                provider_symbol=provider_symbol,
                provider_exchange_id=exchange_id,
                provider_contract_id="NONE",
                provider_instrument_id=instrument_id,
            ),
        ),
        aliases=aliases,
        source_provenance=(
            InstrumentSourceProvenance(
                source_id=TWELVE_DATA_SOURCE.source_id,
                source_record_id=instrument_id,
                observed_at=observed_at,
            ),
        ),
        created_at=observed_at,
        updated_at=observed_at,
    )


def _normalize_exchange_identifiers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TwelveDataPayloadError("available_exchanges is invalid.")
    exchanges: list[str] = []
    for item in value:
        exchange = _optional_text(item)
        if exchange is None:
            raise TwelveDataPayloadError("available_exchanges is invalid.")
        if exchange not in exchanges:
            exchanges.append(exchange)
    return tuple(exchanges)


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
