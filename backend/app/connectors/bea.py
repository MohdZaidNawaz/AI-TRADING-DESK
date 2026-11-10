"""BEA adapter boundary with verified metadata discovery and blocked data mappings."""

from __future__ import annotations

from typing import Any

from app.connectors.macro_adapter import MacroProviderAdapter
from app.connectors.macro_http import MacroHttpTransport
from app.models.macro_intelligence import MacroSourceType
from app.models.macro_providers import (
    MacroMappingStatus,
    MacroProviderBatch,
    MacroProviderConfiguration,
    MacroProviderId,
    MacroProviderRequest,
)
from app.services.macro_provider_registry import mapping_for_indicator


BEA_API_URL = "https://apps.bea.gov/api/data"


class BeaMacroProvider(MacroProviderAdapter):
    """Fail-closed until dataset/table/line mappings are independently verified."""

    provider_id = MacroProviderId.BEA
    source_type = MacroSourceType.OFFICIAL_OBSERVATION

    def __init__(
        self,
        configuration: MacroProviderConfiguration,
        *,
        user_id: str | None,
        transport: MacroHttpTransport | None = None,
    ) -> None:
        super().__init__(configuration)
        self._user_id = user_id.strip() if user_id and user_id.strip() else None
        self._transport = transport or MacroHttpTransport(
            timeout_seconds=configuration.timeout_seconds
        )

    async def fetch(self, request: MacroProviderRequest) -> MacroProviderBatch:
        if request.provider_id is not self.provider_id:
            raise ValueError("BEA request provider mismatch")
        if not self.configuration.enabled or self._user_id is None:
            return self._not_configured(request, "BEA_CREDENTIAL_UNAVAILABLE")
        mappings = [mapping_for_indicator(self.provider_id, item) for item in request.indicator_ids]
        if any(
            item is None or item.status is not MacroMappingStatus.ENABLED
            for item in mappings
        ):
            return self._not_configured(request, "BEA_MAPPING_REQUIRES_METADATA_VERIFICATION")
        return self._not_configured(request, "BEA_DATA_RETRIEVAL_NOT_ACTIVATED")

    async def fetch_metadata(
        self,
        *,
        method: str,
        dataset_name: str | None = None,
    ) -> dict[str, Any]:
        """Explicit owner-invoked metadata call; never used by analysis or refresh."""
        if not self.configuration.enabled or self._user_id is None:
            raise RuntimeError("BEA is not configured")
        allowed = {"GETDATASETLIST", "GETPARAMETERLIST", "GETPARAMETERVALUESFILTERED"}
        normalized = method.upper()
        if normalized not in allowed:
            raise ValueError("unsupported BEA metadata method")
        params: dict[str, str] = {
            "UserID": self._user_id,
            "method": normalized,
            "ResultFormat": "JSON",
        }
        if dataset_name is not None:
            params["datasetname"] = dataset_name
        payload = await self._transport.get_json(BEA_API_URL, params=params)
        if not isinstance(payload, dict) or not isinstance(payload.get("BEAAPI"), dict):
            raise ValueError("BEA metadata response is invalid")
        return payload
