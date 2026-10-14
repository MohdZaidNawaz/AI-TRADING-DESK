"""MT5-specific passive evidence provider for the generic evidence boundary.

The provider never initializes a terminal, logs in, selects symbols, or calls
an execution method.  It can read only from an ``MT5ExecutionAdapter`` that is
already connected by a separately authorized runtime owner.  It is opt-in and
is deliberately not used by default application composition.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.execution.mt5_adapter import MT5ExecutionAdapter
from app.models.trusted_execution_evidence import (
    ExecutionEvidenceAvailability,
    ExecutionEvidenceRequest,
    MarketReferenceObservation,
    ProviderExecutionEvidence,
)


class MT5ReadOnlyExecutionEvidenceProvider:
    """Adapt an already-connected MT5 runtime into sanitized typed evidence."""

    provider_id = "mt5_read_only_runtime"

    def __init__(self, adapter: MT5ExecutionAdapter) -> None:
        self._adapter = adapter

    def read(self, request: ExecutionEvidenceRequest) -> ProviderExecutionEvidence:
        retrieved_at = datetime.now(UTC)
        snapshot, codes = self._adapter.read_only_evidence_snapshot(
            canonical_symbol=request.canonical_symbol,
            broker_symbol=request.broker_symbol,
        )
        if snapshot is None:
            return ProviderExecutionEvidence(
                availability=ExecutionEvidenceAvailability.UNAVAILABLE,
                provider_id=self.provider_id,
                observed_at=retrieved_at,
                retrieved_at=retrieved_at,
                reason_codes=tuple(sorted(set(codes or ("MT5_READ_ONLY_EVIDENCE_UNAVAILABLE",)))),
            )
        return ProviderExecutionEvidence(
            availability=ExecutionEvidenceAvailability.AVAILABLE,
            provider_id=self.provider_id,
            observed_at=snapshot.quote_observed_at,
            retrieved_at=snapshot.retrieved_at,
            instrument=snapshot.instrument,
            market=MarketReferenceObservation(
                canonical_symbol=snapshot.instrument.canonical_symbol,
                broker_symbol=snapshot.instrument.broker_symbol,
                bid=str(snapshot.bid),
                ask=str(snapshot.ask),
                unit=snapshot.account.currency,
                observed_at=snapshot.quote_observed_at,
            ),
            account=snapshot.account,
            positions=snapshot.positions,
            pending_orders=snapshot.pending_orders,
            # MT5 does not establish the Sentinel policy/risk calculation.
            # The generic service will therefore fail closed until an
            # independently verified risk-evidence source is supplied.
            portfolio_risk_inputs=None,
        )
