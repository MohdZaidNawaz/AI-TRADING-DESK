"""Shared adapter contract and safe provider diagnostics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from app.connectors.macro_http import MacroProviderError
from app.models.macro_intelligence import (
    MacroProviderDiagnostic,
    MacroProviderStatus,
    MacroSourceType,
)
from app.models.macro_providers import (
    MacroProviderBatch,
    MacroProviderConfiguration,
    MacroProviderId,
    MacroProviderRequest,
)


class MacroProviderAdapter(ABC):
    provider_id: MacroProviderId
    source_type: MacroSourceType

    def __init__(self, configuration: MacroProviderConfiguration) -> None:
        if configuration.provider_id is not self.provider_id:
            raise ValueError("macro provider configuration identity mismatch")
        self.configuration = configuration

    @abstractmethod
    async def fetch(self, request: MacroProviderRequest) -> MacroProviderBatch:
        """Perform a bounded explicit retrieval with no retries."""

    def _not_configured(
        self,
        request: MacroProviderRequest,
        *reason_codes: str,
    ) -> MacroProviderBatch:
        return MacroProviderBatch(
            provider_id=self.provider_id,
            operation=request.operation,
            diagnostic=MacroProviderDiagnostic(
                provider_id=None,
                source_type=self.source_type,
                status=MacroProviderStatus.NOT_CONFIGURED,
                requested_at=request.requested_at,
                request_count=0,
                reason_codes=tuple(sorted(set(reason_codes or ("PROVIDER_NOT_CONFIGURED",)))),
                error_category="configuration",
                failure_stage="configuration",
                request_sent=False,
            ),
            cache_preserved=True,
        )

    def _failure(
        self,
        request: MacroProviderRequest,
        error: MacroProviderError,
    ) -> MacroProviderBatch:
        status = {
            "rate_limit": MacroProviderStatus.RATE_LIMITED,
            "timeout": MacroProviderStatus.TIMEOUT,
            "network": MacroProviderStatus.NETWORK_ERROR,
            "malformed_response": MacroProviderStatus.MALFORMED,
        }.get(error.category.value, MacroProviderStatus.UNAVAILABLE)
        return MacroProviderBatch(
            provider_id=self.provider_id,
            operation=request.operation,
            diagnostic=MacroProviderDiagnostic(
                provider_id=self.provider_id.value,
                source_type=self.source_type,
                status=status,
                requested_at=request.requested_at,
                retrieved_at=datetime.now(UTC),
                request_count=1 if error.request_sent else 0,
                received_count=0,
                validated_count=0,
                reason_codes=(error.reason_code,),
                error_category=error.category.value,
                failure_stage=error.failure_stage,
                http_status_code=error.http_status_code,
                request_sent=error.request_sent,
                retry_after_seconds=error.retry_after_seconds,
            ),
            cache_preserved=True,
        )
