"""U.S. Treasury nominal and real daily yield-curve adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from xml.etree import ElementTree

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


TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)

_DATASET_BY_PREFIX = {
    "us_nominal_treasury_yield_": "daily_treasury_yield_curve",
    "us_real_treasury_yield_": "daily_treasury_real_yield_curve",
}
_FIELD_BY_MATURITY = {
    "1M": "BC_1MONTH",
    "3M": "BC_3MONTH",
    "6M": "BC_6MONTH",
    "1Y": "BC_1YEAR",
    "2Y": "BC_2YEAR",
    "5Y": "BC_5YEAR",
    "10Y": "BC_10YEAR",
    "20Y": "BC_20YEAR",
    "30Y": "BC_30YEAR",
}
_REAL_FIELD_BY_MATURITY = {"5Y": "TC_5YEAR", "10Y": "TC_10YEAR", "30Y": "TC_30YEAR"}


class TreasuryMacroProvider(MacroProviderAdapter):
    provider_id = MacroProviderId.US_TREASURY
    source_type = MacroSourceType.OFFICIAL_OBSERVATION

    def __init__(
        self,
        configuration: MacroProviderConfiguration,
        *,
        transport: MacroHttpTransport | None = None,
    ) -> None:
        super().__init__(configuration)
        self._transport = transport or MacroHttpTransport(
            timeout_seconds=configuration.timeout_seconds
        )

    async def fetch(self, request: MacroProviderRequest) -> MacroProviderBatch:
        if request.provider_id is not self.provider_id:
            raise ValueError("Treasury request provider mismatch")
        if not self.configuration.enabled:
            return self._not_configured(request, "TREASURY_PROVIDER_DISABLED")

        mappings = []
        for indicator_id in request.indicator_ids:
            mapping = mapping_for_indicator(self.provider_id, indicator_id)
            if mapping is None or mapping.status is not MacroMappingStatus.ENABLED:
                return self._not_configured(request, "TREASURY_MAPPING_UNAVAILABLE")
            mappings.append(mapping)
        datasets = sorted({_dataset(item.indicator_id) for item in mappings})
        if len(datasets) > self.configuration.request_budget:
            return self._not_configured(request, "REQUEST_BUDGET_EXCEEDED")

        retrieved_at = datetime.now(UTC)
        observations = []
        received_count = 0
        request_count = 0
        try:
            for dataset in datasets:
                params: dict[str, str | int] = {"type": dataset, "page": 0}
                if request.start_date is not None and request.end_date is not None:
                    if request.start_date.year == request.end_date.year:
                        params["field_tdr_date_value"] = request.start_date.year
                payload = await self._transport.get_xml(TREASURY_XML_URL, params=params)
                request_count += 1
                records = _records(payload)
                received_count += len(records)
                for record in records:
                    reference_date = _record_date(record)
                    if request.start_date is not None and reference_date < request.start_date:
                        continue
                    if request.end_date is not None and reference_date > request.end_date:
                        continue
                    for mapping in mappings:
                        if _dataset(mapping.indicator_id) != dataset:
                            continue
                        maturity = mapping.indicator_id.rsplit("_", 1)[-1].upper()
                        field = (
                            _REAL_FIELD_BY_MATURITY.get(maturity)
                            if dataset == "daily_treasury_real_yield_curve"
                            else _FIELD_BY_MATURITY.get(maturity)
                        )
                        if field and record.get(field) not in {None, ""}:
                            observations.append(
                                observation_from_mapping(
                                    mapping,
                                    provider_name="U.S. Treasury",
                                    reference_date=reference_date,
                                    raw_value=record[field],
                                    retrieved_at=retrieved_at,
                                    observed_at=datetime.combine(reference_date, time.min, UTC),
                                )
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
        except (ElementTree.ParseError, TypeError, ValueError, KeyError):
            return self._failure(
                request,
                MacroProviderError(
                    MacroProviderFailureCategory.MALFORMED_RESPONSE,
                    reason_code="TREASURY_XML_INVALID",
                    failure_stage="response_validation",
                    request_sent=request_count > 0,
                ),
            )

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


def _dataset(indicator_id: str) -> str:
    for prefix, dataset in _DATASET_BY_PREFIX.items():
        if indicator_id.startswith(prefix):
            return dataset
    raise ValueError("unsupported Treasury indicator")


def _records(payload: bytes) -> list[dict[str, str]]:
    root = ElementTree.fromstring(payload)
    records: list[dict[str, str]] = []
    for properties in root.findall(".//{*}properties"):
        record = {
            child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
            for child in list(properties)
        }
        records.append(record)
    return records


def _record_date(record: dict[str, str]) -> date:
    raw = record.get("NEW_DATE") or record.get("QUOTE_DATE") or record.get("record_date")
    if raw is None:
        raise ValueError("Treasury record date is missing")
    return date.fromisoformat(raw[:10])
