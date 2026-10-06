import pytest

from app.models.proposals import PositionSizingIntent, ProposedTradeParameters
from app.services.execution_validation import validate
from tests.test_execution import approved_execution_pair


def test_validation_accepts_exact_registered_approval_lineage() -> None:
    proposal, decision, _registry, now = approved_execution_pair()
    assert validate(proposal, decision, now) == ()


@pytest.mark.parametrize(
    ("proposal_change", "decision_change", "expected"),
    [
        ({"proposal_id": "other"}, {}, "APPROVAL_PROPOSAL_MISMATCH"),
        ({"analysis_id": "other"}, {}, "APPROVAL_ANALYSIS_MISMATCH"),
        ({"evidence_fingerprint": "z" * 64}, {}, "APPROVAL_EVIDENCE_MISMATCH"),
    ],
)
def test_validation_fails_closed_for_lineage_mismatches(
    proposal_change: dict[str, object], decision_change: dict[str, object], expected: str
) -> None:
    proposal, decision, _registry, now = approved_execution_pair()
    proposal = proposal.model_copy(update=proposal_change)
    decision = decision.model_copy(update=decision_change)
    assert expected in validate(proposal, decision, now)


def test_validation_rejects_expired_proposal_and_approval() -> None:
    proposal, decision, _registry, now = approved_execution_pair()
    assert "PROPOSAL_EXPIRED" in validate(proposal.model_copy(update={"expires_at": now}), decision, now)
    assert "APPROVAL_EXPIRED" in validate(proposal, decision, decision.expires_at)


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        (ProposedTradeParameters(), "MISSING_DIRECTION"),
        (ProposedTradeParameters(direction=None), "MISSING_DIRECTION"),
        (ProposedTradeParameters(direction=approved_execution_pair()[0].parameters.direction), "MISSING_ENTRY"),
        (ProposedTradeParameters(direction=approved_execution_pair()[0].parameters.direction, reference_price=100.0), "MISSING_STOP"),
        (ProposedTradeParameters(direction=approved_execution_pair()[0].parameters.direction, reference_price=100.0, stop_loss=95.0), "MISSING_TARGET"),
        (
            ProposedTradeParameters(
                direction=approved_execution_pair()[0].parameters.direction,
                reference_price=100.0, stop_loss=95.0, targets=(110.0,),
            ),
            "MISSING_EXECUTION_PARAMETER",
        ),
        (
            ProposedTradeParameters(
                direction=approved_execution_pair()[0].parameters.direction,
                reference_price=100.0, stop_loss=95.0, targets=(110.0,),
                position_sizing_intent=PositionSizingIntent.RISK_FRACTION,
            ),
            "MISSING_RISK_FRACTION",
        ),
    ],
)
def test_validation_requires_explicit_trade_and_execution_parameters(
    parameters: ProposedTradeParameters, expected: str
) -> None:
    proposal, decision, _registry, now = approved_execution_pair()
    changed = proposal.model_copy(update={"parameters": parameters})
    assert expected in validate(changed, decision, now)


def test_validation_rejects_ambiguous_account_binding() -> None:
    proposal, decision, _registry, now = approved_execution_pair()
    unbound = decision.model_copy(update={"account_id": None})
    assert "ACCOUNT_BINDING_AMBIGUOUS" in validate(proposal, unbound, now)
