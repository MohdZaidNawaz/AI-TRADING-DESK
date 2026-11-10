"""FRED/ALFRED observations and vintage-aware normalization."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from typing import Any

from app.connectors.macro_adapter import MacroProviderAdapter
from app.connectors.macro_http import MacroHttpTransport, MacroProviderError
from app.connectors.macro_normalization import deduplicate_observations, observation_from_mapping
from app.models.macro_intelligence import MacroProviderDiagnostic, MacroProviderStatus, MacroSourceType
from app.models.macro_providers import (
    MacroMappingStatus,
    MacroProviderBatch,
    MacroProviderConfiguration,
    MacroProviderId,
    MacroProviderRequest,
)
from app.services.macro_provider_registry import mapping_for_indicator


FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"
FRED_VINTAGE_DATES_URL = "https://api.stlouisfed.org/fred/series/vintagedates"


class FredMacroProvider(MacroProviderAdapter):
    provider_id = MacroProviderId.FRED
    source_type = MacroSourceType.OFFICIAL_OBSERVATION

    def __init__(
        self,
        configuration: MacroProviderConfiguration,
        *,
        api_key: str | None,
        transport: MacroHttpTransport | None = None,
    ) -> None:
        super().__init__(configuration)
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._transport = transport or MacroHttpTransport(
            timeout_seconds=configuration.timeout_seconds
        )

    async def fetch(self, request: MacroProviderRequest) -> MacroProviderBatch:
        if request.provider_id is not self.provider_id:
            raise ValueError("FRED request provider mismatch")
        if not self.configuration.enabled or self._api_key is None:
            return self._not_configured(request, "FRED_CREDENTIAL_UNAVAILABLE")
        if len(request.indicator_ids) > self.configuration.request_budget:
            return self._not_configured(request, "REQUEST_BUDGET_EXCEEDED")

        retrieved_at = datetime.now(UTC)
        observations = []
        received_count = 0
        request_count = 0
        try:
            for indicator_id in request.indicator_ids:
                mapping = mapping_for_indicator(self.provider_id, indicator_id)
                if mapping is None or mapping.status is not MacroMappingStatus.ENABLED:
                    raise ValueError(f"unsupported FRED indicator: {indicator_id}")
                params: dict[str, str | int] = {
                    "series_id": mapping.provider_series_id or "",
                    "api_key": self._api_key,
                    "file_type": "json",
                    "limit": request.max_records,
                    "sort_order": "asc",
                    "output_type": 2,
                }
                if request.start_date is not None:
                    params["observation_start"] = request.start_date.isoformat()
                if request.end_date is not None:
                    params["observation_end"] = request.end_date.isoformat()
                if request.vintage_date is not None:
                    value = request.vintage_date.isoformat()
                    params["realtime_start"] = value
                    params["realtime_end"] = value
                payload = await self._transport.get_json(FRED_OBSERVATIONS_URL, params=params)
                request_count += 1
                rows = _observation_rows(payload)
                received_count += len(rows)
                observations.extend(
                    _normalize_rows(mapping, rows, retrieved_at=retrieved_at)
                )
        except MacroProviderError as error:
            failed = self._failure(request, error)
            return failed.model_copy(
                update={
                    "diagnostic": failed.diagnostic.model_copy(
                        update={"request_count": request_count + int(error.request_sent)}
                    )
                }
            )
        except (TypeError, ValueError, KeyError):
            error = MacroProviderError(
                category=_malformed_category(),
                reason_code="FRED_RESPONSE_INVALID",
                failure_stage="response_validation",
                request_sent=request_count > 0,
            )
            return self._failure(request, error)

        normalized = deduplicate_observations(observations)
        diagnostic = MacroProviderDiagnostic(
            provider_id=self.provider_id.value,
            source_type=self.source_type,
            status=MacroProviderStatus.AVAILABLE,
            requested_at=request.requested_at,
            retrieved_at=retrieved_at,
            request_count=request_count,
            received_count=received_count,
            validated_count=len(normalized),
            reason_codes=() if normalized else ("NO_OBSERVATIONS",),
            request_sent=request_count > 0,
            new_evidence_count=len(normalized),
        )
        return MacroProviderBatch(
            provider_id=self.provider_id,
            operation=request.operation,
            observations=normalized,
            diagnostic=diagnostic,
            retrieved_at=retrieved_at,
        )

    async def fetch_series_metadata(self, series_id: str) -> dict[str, Any]:
        if not self.configuration.enabled or self._api_key is None:
            raise RuntimeError("FRED is not configured")
        payload = await self._transport.get_json(
            FRED_SERIES_URL,
            params={"series_id": series_id, "api_key": self._api_key, "file_type": "json"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("seriess"), list):
            raise ValueError("FRED series metadata response is invalid")
        return dict(payload["seriess"][0]) if payload["seriess"] else {}

    async def fetch_vintage_dates(self, series_id: str, *, limit: int = 1000) -> tuple[date, ...]:
        if not self.configuration.enabled or self._api_key is None:
            raise RuntimeError("FRED is not configured")
        payload = await self._transport.get_json(
            FRED_VINTAGE_DATES_URL,
            params={
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "limit": min(max(limit, 1), 1000),
            },
        )
        values = payload.get("vintage_dates") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise ValueError("FRED vintage-date response is invalid")
        return tuple(date.fromisoformat(str(item)) for item in values)


def _observation_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        raise ValueError("FRED observations response is invalid")
    return [dict(item) for item in payload["observations"] if isinstance(item, dict)]


def _normalize_rows(mapping, rows: list[dict[str, Any]], *, retrieved_at: datetime):
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("value") in {None, "."}:
            continue
        grouped[date.fromisoformat(str(row["date"]))].append(row)
    normalized = []
    for reference_date in sorted(grouped):
        previous_id: str | None = None
        unique_rows: dict[str, dict[str, Any]] = {}
        for row in grouped[reference_date]:
            identity = str(row.get("realtime_start", ""))
            existing = unique_rows.get(identity)
            if existing is not None and str(existing.get("value")) != str(row.get("value")):
                raise ValueError("FRED returned conflicting values for one vintage")
            unique_rows[identity] = row
        ordered = sorted(unique_rows.values(), key=lambda item: str(item.get("realtime_start", "")))
        for version, row in enumerate(ordered, start=1):
            vintage_raw = row.get("realtime_start")
            vintage_at = (
                datetime.combine(date.fromisoformat(str(vintage_raw)), time.min, UTC)
                if vintage_raw
                else None
            )
            observation = observation_from_mapping(
                mapping,
                provider_name="FRED/ALFRED",
                reference_date=reference_date,
                raw_value=row["value"],
                retrieved_at=retrieved_at,
                observed_at=datetime.combine(reference_date, time.min, UTC),
                vintage_at=vintage_at,
                observation_version=version,
                revision_of_observation_id=previous_id,
            )
            normalized.append(observation)
            previous_id = observation.observation_id
    return normalized


def _malformed_category():
    from app.models.macro_providers import MacroProviderFailureCategory

    return MacroProviderFailureCategory.MALFORMED_RESPONSE
