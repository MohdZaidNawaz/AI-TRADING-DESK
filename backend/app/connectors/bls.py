"""BLS CPI and labour-series adapter supporting explicit v1 and v2 modes."""

from __future__ import annotations

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
    MacroProviderFailureCategory,
    MacroProviderId,
    MacroProviderRequest,
)
from app.services.macro_provider_registry import mapping_for_indicator


BLS_V1_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_V2_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


class BlsMacroProvider(MacroProviderAdapter):
    provider_id = MacroProviderId.BLS
    source_type = MacroSourceType.OFFICIAL_OBSERVATION

    def __init__(
        self,
        configuration: MacroProviderConfiguration,
        *,
        api_key: str | None = None,
        transport: MacroHttpTransport | None = None,
    ) -> None:
        super().__init__(configuration)
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._transport = transport or MacroHttpTransport(
            timeout_seconds=configuration.timeout_seconds
        )

    async def fetch(self, request: MacroProviderRequest) -> MacroProviderBatch:
        if request.provider_id is not self.provider_id:
            raise ValueError("BLS request provider mismatch")
        if not self.configuration.enabled:
            return self._not_configured(request, "BLS_PROVIDER_DISABLED")
        mappings = []
        for indicator_id in request.indicator_ids:
            mapping = mapping_for_indicator(self.provider_id, indicator_id)
            if mapping is None or mapping.status is not MacroMappingStatus.ENABLED:
                return self._not_configured(request, "BLS_MAPPING_UNAVAILABLE")
            mappings.append(mapping)
        maximum = 50 if self._api_key is not None else 25
        if len(mappings) > min(maximum, self.configuration.request_budget):
            return self._not_configured(request, "REQUEST_BUDGET_EXCEEDED")

        payload: dict[str, Any] = {
            "seriesid": [item.provider_series_id for item in mappings],
        }
        if request.start_date is not None:
            payload["startyear"] = str(request.start_date.year)
        if request.end_date is not None:
            payload["endyear"] = str(request.end_date.year)
        if self._api_key is not None:
            payload["registrationkey"] = self._api_key
        url = BLS_V2_URL if self._api_key is not None else BLS_V1_URL
        retrieved_at = datetime.now(UTC)
        try:
            response = await self._transport.post_json(url, payload=payload)
            series = _series_rows(response)
            observations = []
            received_count = 0
            footnote_count = 0
            mapping_by_series = {item.provider_series_id: item for item in mappings}
            for item in series:
                mapping = mapping_by_series.get(str(item.get("seriesID")))
                if mapping is None:
                    continue
                rows = item.get("data")
                if not isinstance(rows, list):
                    raise ValueError("BLS series data is invalid")
                received_count += len(rows)
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("BLS observation is invalid")
                    period = str(row.get("period", ""))
                    if period == "M13":
                        continue  # annual average is not a monthly observation
                    if len(period) != 3 or not period.startswith("M"):
                        continue
                    month = int(period[1:])
                    if month not in range(1, 13):
                        raise ValueError("BLS month code is invalid")
                    reference_date = date(int(str(row["year"])), month, 1)
                    if request.start_date and reference_date < request.start_date.replace(day=1):
                        continue
                    if request.end_date and reference_date > request.end_date:
                        continue
                    footnotes = row.get("footnotes")
                    if isinstance(footnotes, list) and any(
                        isinstance(note, dict) and note.get("text") for note in footnotes
                    ):
                        footnote_count += 1
                    observations.append(
                        observation_from_mapping(
                            mapping,
                            provider_name="U.S. Bureau of Labor Statistics",
                            reference_date=reference_date,
                            raw_value=row["value"],
                            retrieved_at=retrieved_at,
                            observed_at=datetime.combine(reference_date, time.min, UTC),
                        )
                    )
        except MacroProviderError as error:
            return self._failure(request, error)
        except (TypeError, ValueError, KeyError):
            return self._failure(
                request,
                MacroProviderError(
                    MacroProviderFailureCategory.MALFORMED_RESPONSE,
                    reason_code="BLS_RESPONSE_INVALID",
                    failure_stage="response_validation",
                    request_sent=True,
                ),
            )

        normalized = deduplicate_observations(observations)
        reasons = []
        if not normalized:
            reasons.append("NO_OBSERVATIONS")
        if footnote_count:
            reasons.append("SOURCE_FOOTNOTES_PRESENT")
        diagnostic = MacroProviderDiagnostic(
            provider_id=self.provider_id.value,
            source_type=self.source_type,
            status=MacroProviderStatus.AVAILABLE,
            requested_at=request.requested_at,
            retrieved_at=retrieved_at,
            request_count=1,
            received_count=received_count,
            validated_count=len(normalized),
            reason_codes=tuple(reasons),
            request_sent=True,
            new_evidence_count=len(normalized),
        )
        return MacroProviderBatch(
            provider_id=self.provider_id,
            operation=request.operation,
            observations=normalized,
            diagnostic=diagnostic,
            retrieved_at=retrieved_at,
        )


def _series_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS request did not succeed")
    results = payload.get("Results")
    if not isinstance(results, dict) or not isinstance(results.get("series"), list):
        raise ValueError("BLS response is missing series")
    return [dict(item) for item in results["series"] if isinstance(item, dict)]
