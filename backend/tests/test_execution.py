from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.events import AssetClass
from app.models.execution import ExecutionIntent, ExecutionStatus
from app.models.proposals import TradeDirection, TradeProposalStatus
from app.services.trade_proposal_workflow import ApprovalRegistry, SentinelRiskGate
from tests.test_trade_proposal_workflow import MOMENT, _proposal, _risk_context, _snapshot


def _intent(**changes: object) -> ExecutionIntent:
    data: dict[str, object] = {
        "proposal_id": "proposal-1", "proposal_fingerprint": "a" * 64,
        "analysis_id": "analysis-1", "symbol": "XAUUSD",
        "asset_class": AssetClass.COMMODITY, "direction": TradeDirection.LONG,
        "requested_entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "requested_risk_fraction": 0.01, "approval_id": "approval-1",
        "policy_id": "policy-1", "policy_fingerprint": "b" * 64,
        "assessment_id": "assessment-1", "assessment_fingerprint": "c" * 64,
        "evidence_fingerprint": "d" * 64, "account_id": "account-1",
        "portfolio_snapshot_id": "portfolio-1", "portfolio_snapshot_fingerprint": "e" * 64,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=1),
    }
    data.update(changes)
    return ExecutionIntent(**data)


def approved_execution_pair():
    """Produce a registered, bound Sentinel approval without adapters or I/O."""
    snapshot = _snapshot()
    pending_proposal = _proposal(snapshot=snapshot)
    original = SentinelRiskGate().evaluate(
        proposal=pending_proposal, snapshot=snapshot, risk_context=_risk_context()
    )
    proposal = pending_proposal.model_copy(update={"status": TradeProposalStatus.APPROVED})
    decision = original.model_copy(
        update={
            "approval_id": None, "account_id": "source-account-1",
            "portfolio_snapshot_id": "portfolio-1", "portfolio_snapshot_fingerprint": "p" * 64,
            "metrics_snapshot_id": "metrics-1", "metrics_snapshot_fingerprint": "m" * 64,
        }
    )
    registry = ApprovalRegistry()
    registered = registry._record_approved_decision(proposal, decision)
    return proposal, registered, registry, MOMENT + timedelta(minutes=1)


def test_intent_is_immutable_and_identity_is_deterministic() -> None:
    first = _intent()
    assert first.execution_intent_id == _intent().execution_intent_id
    assert first.requested_entry == 100.0
    assert first.stop_loss == 95.0
    assert first.take_profit == 110.0
    with pytest.raises(ValidationError):
        first.symbol = "OTHER"


def test_intent_identity_changes_for_explicit_trade_or_approval_lineage() -> None:
    first = _intent()
    assert _intent(direction=TradeDirection.SHORT).execution_intent_id != first.execution_intent_id
    assert _intent(approval_id="another-approval").execution_intent_id != first.execution_intent_id


def test_phase_eight_has_no_submission_or_fill_status() -> None:
    assert ExecutionStatus.ADAPTER_UNAVAILABLE.value == "adapter_unavailable"
    assert {status.value for status in ExecutionStatus}.isdisjoint({"submitted", "filled", "cancelled"})
