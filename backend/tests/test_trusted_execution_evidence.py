"""Controlled-fake coverage for the unavailable-by-default evidence boundary."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from app.agents.orchestrator import create_friday_core
from app.models.trusted_execution_evidence import (
    ExecutionEvidenceAuthenticationStatus, ExecutionEvidenceAvailability,
    ExecutionEvidenceCompleteness, ExecutionEvidenceRequest, MarketReferenceObservation,
    PortfolioRiskInputObservation, ProviderExecutionEvidence,
)
from app.models.mt5 import (
    BrokerInstrumentContract,
    MT5AccountSnapshot,
    MT5OrderStatusSnapshot,
    MT5PositionSnapshot,
)
from app.models.portfolio_risk import SourceAccountKey
from app.services.readiness import OperationalReadinessChecker, ReadinessStatus
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
ACCOUNT = SourceAccountKey(
    source_id="controlled-test-source",
    source_account_id="controlled-test-account",
    account_reference="controlled-test-server",
)


def _instrument(**changes: object) -> BrokerInstrumentContract:
    values: dict[str, object] = {
        "canonical_symbol": "XAUUSD",
        "broker_symbol": "XAUUSD",
        "digits": 2,
        "point": 0.01,
        "tick_size": 0.01,
        "minimum_volume": 0.01,
        "maximum_volume": 100.0,
        "volume_step": 0.01,
        "filling_modes": ("FOK", "IOC"),
        "order_modes": ("MARKET",),
        "tradable": True,
        "quote_available": True,
    }
    values.update(changes)
    return BrokerInstrumentContract(**values)


def _account(account: SourceAccountKey = ACCOUNT, **changes: object) -> MT5AccountSnapshot:
    values: dict[str, object] = {
        "account": account,
        "balance": 1000.0,
        "equity": 1000.0,
        "margin": 0.0,
        "free_margin": 1000.0,
        "currency": "USD",
        "trading_allowed": False,
        "terminal_trade_allowed": False,
    }
    values.update(changes)
    return MT5AccountSnapshot(**values)


def _position(**changes: object) -> MT5PositionSnapshot:
    values: dict[str, object] = {
        "ticket": 1,
        "symbol": "XAUUSD",
        "volume": 0.01,
        "price_open": 100.0,
    }
    values.update(changes)
    return MT5PositionSnapshot(**values)


class _FakeProvider:
    provider_id = "controlled_test_read_only_source"

    def __init__(self, response: ProviderExecutionEvidence) -> None:
        self.response = response

    def read(self, request: ExecutionEvidenceRequest) -> ProviderExecutionEvidence:
        del request
        return self.response


def _request(**changes: object) -> ExecutionEvidenceRequest:
    values: dict[str, object] = {"account": ACCOUNT, "canonical_symbol": "XAUUSD", "broker_symbol": "XAUUSD", "max_age_seconds": 30}
    values.update(changes)
    return ExecutionEvidenceRequest(**values)


def _response(**changes: object) -> ProviderExecutionEvidence:
    positions = ()
    values: dict[str, object] = {
        "availability": ExecutionEvidenceAvailability.AVAILABLE,
        "provider_id": _FakeProvider.provider_id,
        "observed_at": NOW,
        "instrument": _instrument(),
        "market": MarketReferenceObservation(canonical_symbol="XAUUSD", broker_symbol="XAUUSD", bid="100.00", ask="100.10", unit="USD", observed_at=NOW),
        "account": _account(),
        "positions": positions,
        "portfolio_risk_inputs": PortfolioRiskInputObservation(
            account=ACCOUNT,
            position_snapshot_fingerprint=_positions_fingerprint(positions),
            policy_id="controlled-test-policy",
            policy_fingerprint="a" * 64,
            observed_at=NOW,
        ),
    }
    values.update(changes)
    return ProviderExecutionEvidence(**values)


def _positions_fingerprint(positions: tuple[object, ...]) -> str:
    serialized = [
        position.model_dump(mode="json")
        for position in sorted(positions, key=lambda item: item.ticket)
    ]
    return sha256(json.dumps(serialized, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def test_unavailable_default_never_fabricates_or_authorizes() -> None:
    result = TrustedExecutionEvidenceService().assess(_request(), now=NOW)
    assert result.ready is False and result.execution_authority is False
    assert result.instrument is result.market is result.account is None
    assert result.reason_codes == ("EXECUTION_EVIDENCE_PROVIDER_UNCONFIGURED",)


def test_fresh_provider_observations_are_validated_with_stable_lineage() -> None:
    service = TrustedExecutionEvidenceService(_FakeProvider(_response()))
    first = service.assess(_request(), now=NOW)
    second = service.assess(_request(), now=NOW)
    assert first.ready is True and first.fingerprint == second.fingerprint
    assert first.provider_authentication_status is ExecutionEvidenceAuthenticationStatus.UNAUTHENTICATED
    assert first.snapshot_completeness is ExecutionEvidenceCompleteness.STRUCTURALLY_COMPLETE
    assert first.execution_authority is False


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"observed_at": NOW - timedelta(minutes=1)}, "EXECUTION_EVIDENCE_STALE"),
        ({"instrument": None}, "EXECUTION_EVIDENCE_INSTRUMENT_MISSING"),
        ({"instrument": _instrument(broker_symbol="XAUUSD.a")}, "EXECUTION_EVIDENCE_INSTRUMENT_MISMATCH"),
        ({"instrument": _instrument(minimum_volume=1, maximum_volume=0.1)}, "EXECUTION_EVIDENCE_INSTRUMENT_CONSTRAINTS_INVALID"),
        ({"market": MarketReferenceObservation(canonical_symbol="XAUUSD", broker_symbol="XAUUSD", bid="101", ask="102", unit="USD", observed_at=NOW-timedelta(minutes=1))}, "EXECUTION_EVIDENCE_MARKET_STALE"),
        ({"market": None}, "EXECUTION_EVIDENCE_MARKET_MISSING"),
        ({"account": None}, "EXECUTION_EVIDENCE_ACCOUNT_MISSING"),
        ({"account": _account(ACCOUNT.model_copy(update={"source_account_id": "other"}))}, "EXECUTION_EVIDENCE_ACCOUNT_MISMATCH"),
        ({"positions": (_position(symbol="OTHER"),)}, "EXECUTION_EVIDENCE_POSITIONS_INCONSISTENT"),
        ({"portfolio_risk_inputs": None}, "EXECUTION_EVIDENCE_PORTFOLIO_RISK_MISSING"),
        ({"portfolio_risk_inputs": PortfolioRiskInputObservation(account=ACCOUNT, position_snapshot_fingerprint="b" * 64, policy_id="controlled-test-policy", policy_fingerprint="a" * 64, observed_at=NOW)}, "EXECUTION_EVIDENCE_PORTFOLIO_RISK_LINEAGE_MISMATCH"),
    ],
)
def test_invalid_or_conflicting_provider_observations_fail_closed(change: dict[str, object], code: str) -> None:
    result = TrustedExecutionEvidenceService(_FakeProvider(_response(**change))).assess(_request(), now=NOW)
    assert result.ready is False and code in result.reason_codes


def test_unavailable_observation_cannot_contain_fabricated_facts() -> None:
    with pytest.raises(ValidationError):
        ProviderExecutionEvidence(availability=ExecutionEvidenceAvailability.UNAVAILABLE, provider_id="fake", observed_at=NOW, reason_codes=("UNAVAILABLE",), instrument=_instrument())


def test_risk_inputs_reject_caller_supplied_safety_assertions_and_incomplete_accounts() -> None:
    with pytest.raises(ValidationError):
        PortfolioRiskInputObservation(
            account=ACCOUNT,
            position_snapshot_fingerprint="a" * 64,
            policy_id="controlled-test-policy",
            policy_fingerprint="b" * 64,
            observed_at=NOW,
            safe=True,
        )
    with pytest.raises(ValidationError):
        MT5AccountSnapshot(
            account=ACCOUNT,
            balance=1000.0,
            equity=1000.0,
            margin=0.0,
            currency="USD",
            trading_allowed=False,
            terminal_trade_allowed=False,
        )


def test_inconsistent_pending_order_data_fails_closed() -> None:
    pending = MT5OrderStatusSnapshot(
        ticket=2,
        symbol="OTHER",
        volume_initial=0.01,
        volume_current=0.01,
        price_open=100.0,
        state="PLACED",
    )
    result = TrustedExecutionEvidenceService(_FakeProvider(_response(pending_orders=(pending,)))).assess(
        _request(), now=NOW
    )
    assert result.ready is False
    assert "EXECUTION_EVIDENCE_PENDING_ORDERS_INCONSISTENT" in result.reason_codes


def test_quote_and_result_models_reject_unsafe_values() -> None:
    with pytest.raises(ValidationError):
        MarketReferenceObservation(canonical_symbol="XAUUSD", broker_symbol="XAUUSD", bid="101", ask="100", unit="USD", observed_at=NOW)
    result = TrustedExecutionEvidenceService(_FakeProvider(_response())).assess(_request(), now=NOW)
    with pytest.raises(ValidationError):
        result.execution_authority = True


def test_provider_errors_and_provenance_mismatch_fail_closed() -> None:
    class _Raises:
        provider_id = "broken"
        def read(self, request):
            del request
            raise TimeoutError
    assert "EXECUTION_EVIDENCE_PROVIDER_UNAVAILABLE" in TrustedExecutionEvidenceService(_Raises()).assess(_request(), now=NOW).reason_codes
    mismatched = _response(provider_id="other")
    assert "EXECUTION_EVIDENCE_PROVENANCE_MISMATCH" in TrustedExecutionEvidenceService(_FakeProvider(mismatched)).assess(_request(), now=NOW).reason_codes


def test_requested_order_and_filling_capabilities_are_validated() -> None:
    result = TrustedExecutionEvidenceService(_FakeProvider(_response())).assess(
        _request(required_order_type="LIMIT", required_filling_mode="BOC"), now=NOW
    )
    assert result.ready is False
    assert "EXECUTION_EVIDENCE_ORDER_TYPE_UNSUPPORTED" in result.reason_codes
    assert "EXECUTION_EVIDENCE_FILLING_MODE_UNSUPPORTED" in result.reason_codes


def test_requested_market_unit_is_provider_validated() -> None:
    result = TrustedExecutionEvidenceService(_FakeProvider(_response())).assess(
        _request(expected_market_unit="EUR"), now=NOW
    )
    assert result.ready is False
    assert "EXECUTION_EVIDENCE_MARKET_UNIT_MISMATCH" in result.reason_codes


def test_portfolio_risk_lineage_must_bind_the_requested_policy() -> None:
    result = TrustedExecutionEvidenceService(_FakeProvider(_response())).assess(
        _request(
            required_risk_policy_id="another-policy",
            required_risk_policy_fingerprint="c" * 64,
        ),
        now=NOW,
    )
    assert result.ready is False
    assert "EXECUTION_EVIDENCE_PORTFOLIO_RISK_POLICY_MISMATCH" in result.reason_codes


def test_core_composes_unavailable_boundary_and_readiness_blocks_when_required() -> None:
    assert create_friday_core().trusted_execution_evidence.provider_id == "unconfigured_read_only_execution_evidence"
    from app.health.models import ComponentHealth, HealthStatus
    from app.services.config_validation import ConfigurationValidation
    report = OperationalReadinessChecker().evaluate(
        configuration=ConfigurationValidation(valid=True, execution_mode="dry_run", audit_path="test", operation_timeout_seconds=1.0, reason_codes=()), health=(), kill_switch_active=False,
        execution_records=(), sentinel_available=True, audit_available=True,
        require_trusted_evidence=True,
        trusted_evidence=TrustedExecutionEvidenceService().assess(_request(), now=NOW),
    )
    assert report.status is ReadinessStatus.NOT_READY
    assert "TRUSTED_EXECUTION_EVIDENCE_UNAVAILABLE" in report.reason_codes
