"""Explicit, single-flight macro retrieval separated from analysis requests."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Awaitable, Callable

from app.config import (
    get_bea_api_user_id,
    get_bls_api_key,
    get_fred_api_key,
    get_macro_enabled_providers,
    get_trading_economics_api_key,
    get_trading_economics_authorizations,
)
from app.connectors.bea import BeaMacroProvider
from app.connectors.bls import BlsMacroProvider
from app.connectors.federal_reserve import FederalReserveMacroProvider
from app.connectors.fred import FredMacroProvider
from app.connectors.macro_adapter import MacroProviderAdapter
from app.connectors.trading_economics import TradingEconomicsMacroProvider
from app.connectors.us_treasury import TreasuryMacroProvider
from app.evidence.store import EvidenceStore
from app.models.macro_intelligence import MacroProviderDiagnostic, MacroProviderStatus
from app.models.macro_providers import (
    MacroProviderConfiguration,
    MacroProviderId,
    MacroProviderOperation,
    MacroProviderRequest,
    MacroRefreshOutcome,
)


class MacroRetrievalService:
    """Owner-invoked retrieval only; it contains no automatic scheduler."""

    def __init__(
        self,
        *,
        evidence_store: EvidenceStore,
        adapters: dict[MacroProviderId, MacroProviderAdapter],
    ) -> None:
        self._evidence_store = evidence_store
        self._adapters = dict(adapters)
        self._coordination_lock = asyncio.Lock()
        self._inflight: dict[tuple[object, ...], asyncio.Task[MacroRefreshOutcome]] = {}
        self._last_outcome: dict[MacroProviderId, MacroRefreshOutcome] = {}

    async def refresh(
        self,
        provider_id: MacroProviderId,
        indicator_ids: tuple[str, ...],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        max_records: int = 500,
    ) -> MacroRefreshOutcome:
        return await self._single_flight(
            MacroProviderRequest(
                provider_id=provider_id,
                operation=MacroProviderOperation.REFRESH,
                indicator_ids=indicator_ids,
                start_date=start_date,
                end_date=end_date,
                max_records=max_records,
                requested_at=datetime.now(UTC),
            )
        )

    async def historical_bootstrap(
        self,
        provider_id: MacroProviderId,
        indicator_ids: tuple[str, ...],
        *,
        start_date: date,
        end_date: date,
        vintage_date: date | None = None,
        max_records: int = 10_000,
    ) -> MacroRefreshOutcome:
        operation = (
            MacroProviderOperation.VINTAGE_SNAPSHOT
            if vintage_date is not None
            else MacroProviderOperation.HISTORICAL_BOOTSTRAP
        )
        return await self._single_flight(
            MacroProviderRequest(
                provider_id=provider_id,
                operation=operation,
                indicator_ids=indicator_ids,
                start_date=start_date,
                end_date=end_date,
                vintage_date=vintage_date,
                max_records=max_records,
                requested_at=datetime.now(UTC),
            )
        )

    def provider_diagnostics(self) -> tuple[MacroProviderDiagnostic, ...]:
        values = []
        for provider_id in MacroProviderId:
            outcome = self._last_outcome.get(provider_id)
            if outcome is not None:
                values.append(outcome.diagnostic)
                continue
            adapter = self._adapters.get(provider_id)
            configured = adapter is not None and adapter.configuration.enabled
            values.append(
                MacroProviderDiagnostic(
                    provider_id=provider_id.value if configured else None,
                    source_type=(
                        adapter.source_type
                        if adapter is not None
                        else _default_source_type(provider_id)
                    ),
                    status=(
                        MacroProviderStatus.NOT_REQUESTED
                        if configured
                        else MacroProviderStatus.NOT_CONFIGURED
                    ),
                    request_count=0,
                    reason_codes=(
                        "PROVIDER_NOT_REQUESTED" if configured else "PROVIDER_NOT_CONFIGURED",
                    ),
                    request_sent=False,
                )
            )
        return tuple(values)

    async def _single_flight(self, request: MacroProviderRequest) -> MacroRefreshOutcome:
        key = (
            request.provider_id,
            request.operation,
            request.indicator_ids,
            request.start_date,
            request.end_date,
            request.vintage_date,
            request.max_records,
        )
        async with self._coordination_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._retrieve_and_ingest(request))
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._coordination_lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _retrieve_and_ingest(
        self,
        request: MacroProviderRequest,
    ) -> MacroRefreshOutcome:
        adapter = self._adapters.get(request.provider_id)
        if adapter is None:
            raise ValueError(f"macro provider adapter is not registered: {request.provider_id.value}")
        before = self._evidence_store.snapshot()
        cached_count = sum(
            len(series.observations)
            for series in before.macro_series
            if series.key.source_id == request.provider_id.value
        ) + sum(
            1
            for item in before.economic_events
            if item.provenance.source_id == request.provider_id.value
        )
        batch = await adapter.fetch(request)
        if not batch.observations and not batch.events:
            diagnostic = batch.diagnostic.model_copy(
                update={"cached_evidence_count": cached_count, "new_evidence_count": 0}
            )
            outcome = MacroRefreshOutcome(
                provider_id=request.provider_id,
                operation=request.operation,
                diagnostic=diagnostic,
                cache_preserved=True,
            )
            self._last_outcome[request.provider_id] = outcome
            return outcome

        observation_ids = []
        event_ids = []
        for observation in batch.observations:
            retained = self._evidence_store.ingest_macro_observation(observation)
            if retained is not None:
                observation_ids.append(observation.observation_id)
        for event in batch.events:
            self._evidence_store.ingest_economic_event(event)
            event_ids.append(event.event_id)
        diagnostic = batch.diagnostic.model_copy(
            update={
                "cached_evidence_count": cached_count,
                "new_evidence_count": len(observation_ids) + len(event_ids),
            }
        )
        outcome = MacroRefreshOutcome(
            provider_id=request.provider_id,
            operation=request.operation,
            diagnostic=diagnostic,
            ingested_observation_ids=tuple(sorted(observation_ids)),
            ingested_event_ids=tuple(sorted(event_ids)),
            cache_preserved=True,
        )
        self._last_outcome[request.provider_id] = outcome
        return outcome


def build_macro_retrieval_service(evidence_store: EvidenceStore) -> MacroRetrievalService:
    """Build configured adapters without starting requests or a scheduler."""
    enabled = get_macro_enabled_providers()
    commercial, redistribution = get_trading_economics_authorizations()
    fred_key = get_fred_api_key()
    bls_key = get_bls_api_key()
    bea_user_id = get_bea_api_user_id()
    te_key = get_trading_economics_api_key()

    def configuration(
        provider: MacroProviderId,
        *,
        credential: bool = False,
        request_budget: int = 10,
    ) -> MacroProviderConfiguration:
        requested_enabled = provider.value in enabled
        if provider is MacroProviderId.TRADING_ECONOMICS:
            requested_enabled = requested_enabled and bool(
                credential and commercial and redistribution
            )
        return MacroProviderConfiguration(
            provider_id=provider,
            enabled=requested_enabled,
            credential_configured=credential,
            commercial_authorization=(commercial if provider is MacroProviderId.TRADING_ECONOMICS else False),
            redistribution_authorization=(redistribution if provider is MacroProviderId.TRADING_ECONOMICS else False),
            request_budget=request_budget,
        )

    adapters: dict[MacroProviderId, MacroProviderAdapter] = {
        MacroProviderId.FRED: FredMacroProvider(
            configuration(MacroProviderId.FRED, credential=fred_key is not None, request_budget=20),
            api_key=fred_key,
        ),
        MacroProviderId.US_TREASURY: TreasuryMacroProvider(
            configuration(MacroProviderId.US_TREASURY, request_budget=2)
        ),
        MacroProviderId.BLS: BlsMacroProvider(
            configuration(MacroProviderId.BLS, credential=bls_key is not None, request_budget=50),
            api_key=bls_key,
        ),
        MacroProviderId.BEA: BeaMacroProvider(
            configuration(MacroProviderId.BEA, credential=bea_user_id is not None),
            user_id=bea_user_id,
        ),
        MacroProviderId.FEDERAL_RESERVE: FederalReserveMacroProvider(
            configuration(MacroProviderId.FEDERAL_RESERVE, request_budget=3)
        ),
        MacroProviderId.TRADING_ECONOMICS: TradingEconomicsMacroProvider(
            configuration(
                MacroProviderId.TRADING_ECONOMICS,
                credential=te_key is not None,
            ),
            api_key=te_key,
        ),
    }
    return MacroRetrievalService(evidence_store=evidence_store, adapters=adapters)


def _default_source_type(provider_id: MacroProviderId):
    from app.models.macro_intelligence import MacroSourceType

    if provider_id is MacroProviderId.FEDERAL_RESERVE:
        return MacroSourceType.CENTRAL_BANK
    if provider_id is MacroProviderId.TRADING_ECONOMICS:
        return MacroSourceType.COMMERCIAL_CONSENSUS
    return MacroSourceType.OFFICIAL_OBSERVATION
