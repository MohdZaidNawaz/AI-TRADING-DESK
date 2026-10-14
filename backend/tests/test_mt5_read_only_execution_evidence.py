"""Fake-runtime tests for the opt-in MT5 passive evidence provider."""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.mt5 import (
    BrokerInstrumentContract,
    MT5AccountSnapshot,
    MT5ReadOnlyEvidenceSnapshot,
)
from app.models.portfolio_risk import SourceAccountKey
from app.models.trusted_execution_evidence import ExecutionEvidenceRequest
from app.services.mt5_read_only_execution_evidence import MT5ReadOnlyExecutionEvidenceProvider
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
ACCOUNT = SourceAccountKey(source_id="mt5", source_account_id="test-account", account_reference="test-server")


class _AlreadyConnectedFakeAdapter:
    def __init__(self, snapshot: MT5ReadOnlyEvidenceSnapshot | None, codes: tuple[str, ...] = ()) -> None:
        self.snapshot = snapshot
        self.codes = codes
        self.calls = 0
        self.initialize_calls = self.login_calls = self.order_send_calls = 0

    def read_only_evidence_snapshot(self, *, canonical_symbol: str, broker_symbol: str):
        self.calls += 1
        assert canonical_symbol == "XAUUSD" and broker_symbol == "XAUUSD"
        return self.snapshot, self.codes


def _request() -> ExecutionEvidenceRequest:
    return ExecutionEvidenceRequest(
        account=ACCOUNT, canonical_symbol="XAUUSD", broker_symbol="XAUUSD", max_age_seconds=30
    )


def _snapshot() -> MT5ReadOnlyEvidenceSnapshot:
    instrument = BrokerInstrumentContract(
        canonical_symbol="XAUUSD", broker_symbol="XAUUSD", digits=2, point=0.01,
        tick_size=0.01, tick_value=1.0, contract_size=100.0, minimum_volume=0.01,
        maximum_volume=10.0, volume_step=0.01, filling_modes=("FOK",), order_modes=("MARKET",),
        visible=True, tradable=True, quote_available=True,
    )
    account = MT5AccountSnapshot(
        account=ACCOUNT, balance=1000.0, equity=1000.0, margin=0.0, free_margin=1000.0,
        currency="USD", trading_allowed=False, terminal_trade_allowed=False,
    )
    return MT5ReadOnlyEvidenceSnapshot(
        account=account, instrument=instrument, bid=100.0, ask=100.1,
        quote_observed_at=NOW, retrieved_at=NOW,
    )


def test_unconnected_runtime_returns_unavailable_without_initialization_or_submission() -> None:
    runtime = _AlreadyConnectedFakeAdapter(None, ("MT5_READ_ONLY_RUNTIME_NOT_CONNECTED",))
    result = MT5ReadOnlyExecutionEvidenceProvider(runtime).read(_request())
    assert result.availability.value == "unavailable"
    assert result.reason_codes == ("MT5_READ_ONLY_RUNTIME_NOT_CONNECTED",)
    assert runtime.calls == 1
    assert runtime.initialize_calls == runtime.login_calls == runtime.order_send_calls == 0


def test_connected_read_only_snapshot_preserves_source_timestamp_and_never_submits() -> None:
    runtime = _AlreadyConnectedFakeAdapter(_snapshot())
    result = MT5ReadOnlyExecutionEvidenceProvider(runtime).read(_request())
    assert result.availability.value == "available"
    assert result.observed_at == NOW and result.retrieved_at == NOW
    assert result.account is not None and result.market is not None
    assert result.portfolio_risk_inputs is None
    assert runtime.initialize_calls == runtime.login_calls == runtime.order_send_calls == 0


def test_mt5_facts_without_independent_risk_input_remain_fail_closed() -> None:
    runtime = _AlreadyConnectedFakeAdapter(_snapshot())
    result = TrustedExecutionEvidenceService(
        MT5ReadOnlyExecutionEvidenceProvider(runtime)
    ).assess(_request(), now=NOW)
    assert result.ready is False
    assert "EXECUTION_EVIDENCE_PORTFOLIO_RISK_MISSING" in result.reason_codes
    assert runtime.initialize_calls == runtime.login_calls == runtime.order_send_calls == 0
