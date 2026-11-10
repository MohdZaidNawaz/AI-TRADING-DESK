"""Bounded Twelve Data historical retrieval for research preparation only."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
import math
from typing import Protocol

from app.connectors.twelve_data import (
    TwelveDataClient,
    TwelveDataConfigurationError,
    TwelveDataHistoricalConnector,
    TWELVE_DATA_HISTORICAL_REQUEST_TIMEZONE,
    TwelveDataPayloadError,
    TwelveDataProviderError,
    twelve_data_historical_connector,
)
from app.models.research_requests import (
    HistoricalRetrievalDiagnostic,
    HistoricalRetrievalStatus,
    InstrumentResolutionStatus,
    ResearchLookback,
)
from app.models.schemas import AnalyzeRequest


DEFAULT_HISTORICAL_OUTPUT_SIZE = 100
# The existing in-memory series store retains at most 500 bars per series.
MAX_RESEARCH_OUTPUT_SIZE = 500
_TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
    "1w": 604_800,
}
_LOOKBACK_SECONDS = {
    "minutes": 60,
    "hours": 3_600,
    "days": 86_400,
    "weeks": 604_800,
    "months": 2_592_000,
    "years": 31_536_000,
}


class HistoricalCore(Protocol):
    def prepare_research_request(self, request: AnalyzeRequest): ...

    def evidence_snapshot(self, *, symbol: str | None = None): ...

    def ingest_market_bar(self, bar): ...


class HistoricalClient(Protocol):
    async def fetch_time_series(
        self,
        symbol: str,
        interval: str,
        *,
        outputsize: int,
        start_date: datetime | None = None,
    ): ...


class TwelveDataHistoricalService:
    """Retrieve XAU/USD candles once, validate all, then stage them in FRIDAY Core."""

    def __init__(
        self,
        *,
        core: HistoricalCore,
        client: HistoricalClient | None,
        connector: TwelveDataHistoricalConnector = twelve_data_historical_connector,
        now=None,
    ) -> None:
        self._core = core
        self._client = client
        self._connector = connector
        self._now = now or (lambda: datetime.now(UTC))
        self._provider_blocked_reason: str | None = None

    async def prepare(self, request: AnalyzeRequest) -> HistoricalRetrievalDiagnostic:
        context = self._core.prepare_research_request(request)
        subject = context.primary_instrument
        if subject.status is not InstrumentResolutionStatus.RESOLVED:
            return self._diagnostic(
                HistoricalRetrievalStatus.UNAVAILABLE,
                reason="HISTORICAL_INSTRUMENT_NOT_RESOLVED",
            )
        if subject.canonical_symbol != "XAU/USD":
            return self._diagnostic(
                HistoricalRetrievalStatus.UNSUPPORTED,
                reason="HISTORICAL_SYMBOL_NOT_SUPPORTED_IN_PHASE_2",
                provider_symbol=subject.canonical_symbol,
                timeframe=context.timeframe,
            )
        if context.timeframe is None:
            return self._diagnostic(
                HistoricalRetrievalStatus.NOT_REQUESTED,
                reason="HISTORICAL_TIMEFRAME_NOT_SPECIFIED",
                provider_symbol="XAU/USD",
            )

        requested_count, lookback_truncated = _requested_output_size(
            context.lookback,
            context.timeframe,
        )
        now = self._now().astimezone(UTC)
        cached = _matching_series(
            self._core.evidence_snapshot(symbol=subject.canonical_symbol).market_series,
            timeframe=context.timeframe,
        )
        if _cache_is_reusable(cached, now=now, requested_count=requested_count):
            series = max(cached, key=lambda item: item.provenance.latest_received_at)
            reasons = ["HISTORICAL_CACHE_REUSED"]
            if lookback_truncated:
                reasons.append("HISTORICAL_LOOKBACK_BOUNDED")
            return HistoricalRetrievalDiagnostic(
                status=HistoricalRetrievalStatus.CACHE_HIT,
                provider_symbol="XAU/USD",
                timeframe=context.timeframe,
                requested_count=requested_count,
                validated_count=series.bar_count,
                retrieved_at=series.provenance.latest_received_at,
                missing_interval_count=series.continuity.missing_interval_count,
                provisional_count=series.provisional_count,
                partial=(
                    lookback_truncated
                    or series.continuity.missing_interval_count > 0
                    or series.provisional_count > 0
                ),
                reason_codes=tuple(reasons),
            )
        if self._provider_blocked_reason is not None:
            return self._diagnostic(
                HistoricalRetrievalStatus.RATE_LIMITED,
                reason=self._provider_blocked_reason,
                provider_symbol="XAU/USD",
                timeframe=context.timeframe,
                requested_count=requested_count,
                error_category="rate_limit",
            )
        if self._client is None:
            return self._diagnostic(
                HistoricalRetrievalStatus.NOT_CONFIGURED,
                reason="TWELVE_DATA_NOT_CONFIGURED",
                provider_symbol="XAU/USD",
                timeframe=context.timeframe,
                requested_count=requested_count,
                error_category="configuration",
            )

        incremental_start = _incremental_start(cached, requested_count=requested_count)
        try:
            payload = await self._client.fetch_time_series(
                "XAU/USD",
                context.timeframe,
                outputsize=requested_count,
                start_date=incremental_start,
            )
            batch = self._connector.normalize(
                payload,
                expected_symbol="XAU/USD",
                timeframe=context.timeframe,
                requested_count=requested_count,
                retrieved_at=now,
                requested_timezone=TWELVE_DATA_HISTORICAL_REQUEST_TIMEZONE,
            )
        except TwelveDataProviderError as exc:
            status = _provider_status(exc.category)
            if exc.category == "rate_limit":
                self._provider_blocked_reason = "TWELVE_DATA_RATE_LIMITED"
            return self._diagnostic(
                status,
                reason=_provider_reason(exc.category),
                provider_symbol="XAU/USD",
                timeframe=context.timeframe,
                requested_count=requested_count,
                retrieved_at=now,
                error_category=exc.category,
            )
        except TwelveDataPayloadError as exc:
            return self._diagnostic(
                HistoricalRetrievalStatus.INVALID_RESPONSE,
                reason="TWELVE_DATA_HISTORICAL_RESPONSE_INVALID",
                provider_symbol="XAU/USD",
                timeframe=context.timeframe,
                requested_count=requested_count,
                retrieved_at=now,
                error_category=exc.category,
            )

        if not batch.bars:
            return HistoricalRetrievalDiagnostic(
                status=HistoricalRetrievalStatus.UNAVAILABLE,
                provider_symbol=batch.provider_symbol,
                timeframe=context.timeframe,
                requested_count=requested_count,
                validated_count=0,
                retrieved_at=batch.retrieved_at,
                partial=True,
                reason_codes=batch.warning_codes,
                error_category="empty_response",
            )

        # No bar is staged until the connector has validated the complete payload.
        for bar in batch.bars:
            self._core.ingest_market_bar(bar)
        combined = _matching_series(
            self._core.evidence_snapshot(symbol=subject.canonical_symbol).market_series,
            timeframe=context.timeframe,
        )
        if not combined:
            return self._diagnostic(
                HistoricalRetrievalStatus.UNAVAILABLE,
                reason="HISTORICAL_CACHE_REJECTED_VALIDATED_BATCH",
                provider_symbol=batch.provider_symbol,
                timeframe=context.timeframe,
                requested_count=requested_count,
                retrieved_at=batch.retrieved_at,
                error_category="storage",
            )
        selected = max(combined, key=lambda item: item.provenance.latest_received_at)
        warnings = list(batch.warning_codes)
        if selected.bar_count >= requested_count:
            warnings = [
                code for code in warnings if code != "HISTORICAL_RESPONSE_SHORT"
            ]
        if selected.continuity.missing_interval_count == 0:
            warnings = [
                code for code in warnings if code != "HISTORICAL_INTERVALS_MISSING"
            ]
        if selected.provisional_count == 0:
            warnings = [
                code
                for code in warnings
                if code != "HISTORICAL_PROVISIONAL_BAR_PRESENT"
            ]
        reason_codes = ["TWELVE_DATA_HISTORICAL_RETRIEVED", *warnings]
        if incremental_start is not None:
            reason_codes.append("HISTORICAL_INCREMENTAL_REFRESH")
        if lookback_truncated:
            reason_codes.append("HISTORICAL_LOOKBACK_BOUNDED")
        partial = (
            lookback_truncated
            or selected.bar_count < requested_count
            or selected.continuity.missing_interval_count > 0
            or selected.provisional_count > 0
        )
        return HistoricalRetrievalDiagnostic(
            status=HistoricalRetrievalStatus.RETRIEVED,
            provider_symbol=batch.provider_symbol,
            timeframe=context.timeframe,
            requested_count=requested_count,
            validated_count=selected.bar_count,
            retrieved_at=batch.retrieved_at,
            missing_interval_count=selected.continuity.missing_interval_count,
            provisional_count=selected.provisional_count,
            partial=partial,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
        )

    @staticmethod
    def _diagnostic(
        status: HistoricalRetrievalStatus,
        *,
        reason: str,
        provider_symbol: str | None = None,
        timeframe: str | None = None,
        requested_count: int = 0,
        retrieved_at: datetime | None = None,
        error_category: str | None = None,
    ) -> HistoricalRetrievalDiagnostic:
        return HistoricalRetrievalDiagnostic(
            status=status,
            provider_symbol=provider_symbol,
            timeframe=timeframe,
            requested_count=requested_count,
            retrieved_at=retrieved_at,
            reason_codes=(reason,),
            error_category=error_category,
        )


def _requested_output_size(
    lookback: ResearchLookback | None,
    timeframe: str,
) -> tuple[int, bool]:
    if lookback is None:
        return DEFAULT_HISTORICAL_OUTPUT_SIZE, False
    if lookback.unit == "bars":
        requested = lookback.value
    else:
        requested = math.ceil(
            lookback.value * _LOOKBACK_SECONDS[lookback.unit] / _TIMEFRAME_SECONDS[timeframe]
        )
    bounded = max(1, min(requested, MAX_RESEARCH_OUTPUT_SIZE))
    return bounded, bounded != requested


def _matching_series(series, *, timeframe: str):
    return tuple(
        item
        for item in series
        if item.key.source_id == "twelve_data" and item.timeframe == timeframe
    )


def _cache_is_reusable(series, *, now: datetime, requested_count: int) -> bool:
    for item in series:
        latest = item.latest_completed_at
        if latest is None or latest > now:
            continue
        if item.bar_count < requested_count:
            continue
        if (now - latest).total_seconds() <= _TIMEFRAME_SECONDS[item.timeframe] * 2:
            return True
    return False


def _incremental_start(series, *, requested_count: int) -> datetime | None:
    candidates = [
        item
        for item in series
        if item.bar_count >= requested_count and item.latest_bar is not None
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: item.provenance.latest_received_at)
    return selected.latest_bar.opened_at


def _provider_status(category: str) -> HistoricalRetrievalStatus:
    if category == "rate_limit":
        return HistoricalRetrievalStatus.RATE_LIMITED
    if category == "timeout":
        return HistoricalRetrievalStatus.TIMEOUT
    return HistoricalRetrievalStatus.PROVIDER_ERROR


def _provider_reason(category: str) -> str:
    return {
        "rate_limit": "TWELVE_DATA_RATE_LIMITED",
        "timeout": "TWELVE_DATA_TIMEOUT",
        "authentication": "TWELVE_DATA_AUTHENTICATION_FAILED",
        "network": "TWELVE_DATA_NETWORK_UNAVAILABLE",
        "server_error": "TWELVE_DATA_SERVER_ERROR",
    }.get(category, "TWELVE_DATA_PROVIDER_ERROR")


@lru_cache
def get_twelve_data_historical_service() -> TwelveDataHistoricalService:
    """Compose the existing secure client; absence remains an explicit diagnostic."""
    from app.services.friday_core import get_friday_core

    try:
        client: HistoricalClient | None = TwelveDataClient.from_environment()
    except TwelveDataConfigurationError:
        client = None
    return TwelveDataHistoricalService(core=get_friday_core(), client=client)
