"""Disabled-by-default commercial macro calendar adapter."""

from __future__ import annotations

from app.connectors.macro_adapter import MacroProviderAdapter
from app.connectors.macro_http import MacroHttpTransport
from app.models.macro_intelligence import MacroSourceType
from app.models.macro_providers import (
    MacroProviderBatch,
    MacroProviderConfiguration,
    MacroProviderId,
    MacroProviderRequest,
)


TRADING_ECONOMICS_CALENDAR_URL = "https://api.tradingeconomics.com/calendar"


class TradingEconomicsMacroProvider(MacroProviderAdapter):
    provider_id = MacroProviderId.TRADING_ECONOMICS
    source_type = MacroSourceType.COMMERCIAL_CONSENSUS

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
            raise ValueError("Trading Economics request provider mismatch")
        if not self.configuration.enabled:
            return self._not_configured(request, "COMMERCIAL_PROVIDER_DISABLED")
        if not (
            self._api_key
            and self.configuration.commercial_authorization
            and self.configuration.redistribution_authorization
        ):
            return self._not_configured(
                request,
                "COMMERCIAL_CREDENTIAL_OR_REDISTRIBUTION_AUTHORIZATION_UNAVAILABLE",
            )
        return self._not_configured(
            request,
            "TRADING_ECONOMICS_SCHEMA_ENTITLEMENT_REQUIRES_VERIFICATION",
        )
