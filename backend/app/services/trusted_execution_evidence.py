"""Fail-closed read-only execution evidence provider boundary.

The default provider is deliberately unavailable. This service performs no
broker I/O itself and cannot issue approvals, authorizations, or reservations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Protocol, runtime_checkable

from app.models.trusted_execution_evidence import (
    ExecutionEvidenceAuthenticationStatus,
    ExecutionEvidenceAvailability,
    ExecutionEvidenceCompleteness,
    ExecutionEvidenceRequest,
    ProviderExecutionEvidence,
    ValidatedExecutionEvidence,
)


@runtime_checkable
class ReadOnlyExecutionEvidenceProvider(Protocol):
    """Configured application source only; test fakes must be injected directly."""

    @property
    def provider_id(self) -> str: ...

    def read(self, request: ExecutionEvidenceRequest) -> ProviderExecutionEvidence: ...


class UnavailableExecutionEvidenceProvider:
    """Concrete no-I/O default that never fabricates source observations."""

    provider_id = "unconfigured_read_only_execution_evidence"

    def read(self, request: ExecutionEvidenceRequest) -> ProviderExecutionEvidence:
        del request
        return ProviderExecutionEvidence(
            availability=ExecutionEvidenceAvailability.UNAVAILABLE,
            provider_id=self.provider_id,
            observed_at=datetime.now(UTC),
            reason_codes=("EXECUTION_EVIDENCE_PROVIDER_UNCONFIGURED",),
        )


class TrustedExecutionEvidenceService:
    """Validate provider-bound facts into a non-authorizing evidence result."""

    def __init__(self, provider: ReadOnlyExecutionEvidenceProvider | None = None) -> None:
        self._provider = provider or UnavailableExecutionEvidenceProvider()

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    def assess(self, request: ExecutionEvidenceRequest, *, now: datetime) -> ValidatedExecutionEvidence:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("evidence assessment timestamp requires timezone awareness")
        now = now.astimezone(UTC)
        try:
            response = self._provider.read(request)
        except Exception:
            return _unavailable(request, self.provider_id, now, "EXECUTION_EVIDENCE_PROVIDER_UNAVAILABLE")
        if not isinstance(response, ProviderExecutionEvidence):
            return _unavailable(request, self.provider_id, now, "EXECUTION_EVIDENCE_PROVIDER_MALFORMED")
        if response.provider_id != self.provider_id:
            return _unavailable(request, self.provider_id, now, "EXECUTION_EVIDENCE_PROVENANCE_MISMATCH")
        if response.availability is not ExecutionEvidenceAvailability.AVAILABLE:
            return _unavailable(request, self.provider_id, now, *response.reason_codes)

        codes = _validate(response, request, now)
        return ValidatedExecutionEvidence(
            request=request,
            provider_id=self.provider_id,
            evaluated_at=now,
            source_observed_at=response.observed_at,
            source_retrieved_at=response.retrieved_at,
            instrument=response.instrument,
            market=response.market,
            account=response.account,
            positions=response.positions,
            pending_orders=response.pending_orders,
            portfolio_risk_inputs=response.portfolio_risk_inputs,
            provider_authentication_status=ExecutionEvidenceAuthenticationStatus.UNAUTHENTICATED,
            snapshot_completeness=(
                ExecutionEvidenceCompleteness.STRUCTURALLY_COMPLETE
                if not codes
                else ExecutionEvidenceCompleteness.INCOMPLETE
            ),
            reason_codes=tuple(codes),
            ready=not codes,
        )


def _unavailable(request: ExecutionEvidenceRequest, provider_id: str, now: datetime, *codes: str) -> ValidatedExecutionEvidence:
    return ValidatedExecutionEvidence(
        request=request,
        provider_id=provider_id,
        evaluated_at=now,
        reason_codes=tuple(
            sorted(set(codes or ("EXECUTION_EVIDENCE_PROVIDER_UNAVAILABLE",)))
        ),
    )


def _validate(response: ProviderExecutionEvidence, request: ExecutionEvidenceRequest, now: datetime) -> list[str]:
    codes: list[str] = []
    if response.observed_at > now or now - response.observed_at > timedelta(seconds=request.max_age_seconds):
        codes.append("EXECUTION_EVIDENCE_STALE")
    if response.retrieved_at is None:
        codes.append("EXECUTION_EVIDENCE_RETRIEVAL_TIMESTAMP_MISSING")
    else:
        if response.retrieved_at < response.observed_at:
            codes.append("EXECUTION_EVIDENCE_RETRIEVAL_CHRONOLOGY_INVALID")
        if (
            response.retrieved_at > now
            or now - response.retrieved_at
            > timedelta(seconds=request.max_age_seconds)
        ):
            codes.append("EXECUTION_EVIDENCE_RETRIEVAL_STALE")
    instrument, market, account = response.instrument, response.market, response.account
    if instrument is None:
        codes.append("EXECUTION_EVIDENCE_INSTRUMENT_MISSING")
    else:
        if instrument.canonical_symbol.upper() != request.canonical_symbol or instrument.broker_symbol.upper() != request.broker_symbol:
            codes.append("EXECUTION_EVIDENCE_INSTRUMENT_MISMATCH")
        if instrument.minimum_volume <= 0 or instrument.maximum_volume < instrument.minimum_volume or instrument.volume_step <= 0:
            codes.append("EXECUTION_EVIDENCE_INSTRUMENT_CONSTRAINTS_INVALID")
        if instrument.tick_size <= 0 or instrument.point <= 0 or not instrument.tradable or not instrument.quote_available:
            codes.append("EXECUTION_EVIDENCE_INSTRUMENT_UNAVAILABLE")
        order_modes = {value.upper() for value in instrument.order_modes}
        filling_modes = {value.upper() for value in instrument.filling_modes}
        if request.required_order_type and request.required_order_type not in order_modes:
            codes.append("EXECUTION_EVIDENCE_ORDER_TYPE_UNSUPPORTED")
        if request.required_filling_mode and request.required_filling_mode not in filling_modes:
            codes.append("EXECUTION_EVIDENCE_FILLING_MODE_UNSUPPORTED")
    if market is None:
        codes.append("EXECUTION_EVIDENCE_MARKET_MISSING")
    else:
        if market.canonical_symbol != request.canonical_symbol or market.broker_symbol != request.broker_symbol:
            codes.append("EXECUTION_EVIDENCE_MARKET_MISMATCH")
        if request.expected_market_unit and market.unit != request.expected_market_unit:
            codes.append("EXECUTION_EVIDENCE_MARKET_UNIT_MISMATCH")
        if market.observed_at > now or now - market.observed_at > timedelta(seconds=request.max_age_seconds):
            codes.append("EXECUTION_EVIDENCE_MARKET_STALE")
        if response.retrieved_at is not None and market.observed_at > response.retrieved_at:
            codes.append("EXECUTION_EVIDENCE_MARKET_CHRONOLOGY_INVALID")
    if account is None:
        codes.append("EXECUTION_EVIDENCE_ACCOUNT_MISSING")
    elif account.account != request.account or account.equity < 0 or account.balance < 0 or account.margin < 0 or account.free_margin < 0:
        codes.append("EXECUTION_EVIDENCE_ACCOUNT_MISMATCH")
    tickets = [position.ticket for position in response.positions]
    if (
        len(tickets) != len(set(tickets))
        or any(position.symbol.upper() != request.broker_symbol for position in response.positions)
        or any(position.volume <= 0 for position in response.positions)
    ):
        codes.append("EXECUTION_EVIDENCE_POSITIONS_INCONSISTENT")
    order_tickets = [order.ticket for order in response.pending_orders]
    if (
        len(order_tickets) != len(set(order_tickets))
        or any(order.symbol.upper() != request.broker_symbol for order in response.pending_orders)
        or any(order.volume_initial <= 0 or order.volume_current < 0 for order in response.pending_orders)
    ):
        codes.append("EXECUTION_EVIDENCE_PENDING_ORDERS_INCONSISTENT")
    risk_inputs = response.portfolio_risk_inputs
    if risk_inputs is None:
        codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_MISSING")
    else:
        if risk_inputs.account != request.account:
            codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_ACCOUNT_MISMATCH")
        if risk_inputs.observed_at > now or now - risk_inputs.observed_at > timedelta(seconds=request.max_age_seconds):
            codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_STALE")
        if (
            response.retrieved_at is not None
            and risk_inputs.observed_at > response.retrieved_at
        ):
            codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_CHRONOLOGY_INVALID")
        if risk_inputs.position_snapshot_fingerprint != _positions_fingerprint(response.positions):
            codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_LINEAGE_MISMATCH")
        if (
            request.required_risk_policy_id
            and risk_inputs.policy_id != request.required_risk_policy_id
        ):
            codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_POLICY_MISMATCH")
        if (
            request.required_risk_policy_fingerprint
            and risk_inputs.policy_fingerprint != request.required_risk_policy_fingerprint
        ):
            codes.append("EXECUTION_EVIDENCE_PORTFOLIO_RISK_POLICY_MISMATCH")
    return sorted(set(codes))


def _positions_fingerprint(positions: tuple[object, ...]) -> str:
    """Stable source-position lineage only; it is not a risk assessment."""
    serialized = [
        position.model_dump(mode="json")
        for position in sorted(positions, key=lambda item: item.ticket)
    ]
    return sha256(
        json.dumps(serialized, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
