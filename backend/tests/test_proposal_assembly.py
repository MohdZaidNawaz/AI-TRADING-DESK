"""Phase 6A-2 tests for explicit, non-authorizing proposal assembly."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.agents import create_friday_core
from app.agents.base import AnalysisLifecycle
from app.execution import PilotApprovalError, PilotExecutionCoordinator
from app.evidence import EvidenceStore
from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    SourceMetadata,
    SourceTransport,
)
from app.models.proposal_assembly import ExplicitTradeProposalInput
from app.models.proposals import (
    PortfolioRiskContext,
    PositionSizingIntent,
    RiskContext,
    RiskReward,
    SentinelDecisionStatus,
    SpecialistSignal,
    TradeDirection,
)
from app.models.schemas import AnalyzeRequest
from app.models.trade_thesis import MasterTradeThesisReference
from app.services.proposal_assembly import ProposalAssemblyError, ProposalAssemblyService
from app.services.trade_proposal_workflow import SentinelRiskGate, proposal_fingerprint


MOMENT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
MARKET_SOURCE = SourceMetadata(
    source_id="phase6a2-market",
    display_name="Phase 6A-2 Market",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)


def _market_event(*, event_id: str = "phase6a2-market-1", price: float = 100.0) -> NormalizedDataEvent:
    return NormalizedDataEvent(
        event_id=event_id,
        source=MARKET_SOURCE,
        event_type=EventType.MARKET_QUOTE,
        event="market_snapshot",
        asset_class=AssetClass.COMMODITY,
        symbol="XAUUSD",
        price=price,
        close=price,
        timestamp=MOMENT,
        raw_payload={"must_not_reach_proposal": "raw"},
    )


def _run():
    core = create_friday_core()
    core.ingest_event(_market_event())
    snapshot = core.evidence_snapshot(symbol="XAUUSD")
    request = AnalyzeRequest(symbol="XAUUSD", query="Review market structure and risk")
    run = core._analyze_run(  # noqa: SLF001 - verifies the internal-only boundary.
        request,
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    return core, request, snapshot, run


def _input(thesis, **updates: object) -> ExplicitTradeProposalInput:
    values: dict[str, object] = {
        "input_id": "human-input-1",
        "master_thesis_reference": thesis.as_reference(),
        "direction": TradeDirection.LONG,
        "reference_price": 100.0,
        "stop_loss": 95.0,
        "targets": (110.0,),
        "risk_reward": RiskReward(risk_distance=5.0, reward_distance=10.0, ratio=2.0),
        "position_sizing_intent": PositionSizingIntent.RISK_FRACTION,
        "requested_risk_fraction": 0.01,
        "rationale": "Explicit human-supplied research proposal.",
        "submitted_at": MOMENT + timedelta(minutes=1),
    }
    values.update(updates)
    return ExplicitTradeProposalInput(**values)


def _risk_context() -> RiskContext:
    return RiskContext(
        as_of=MOMENT + timedelta(minutes=1),
        max_market_evidence_age_seconds=600,
        approval_ttl_seconds=120,
        portfolio=PortfolioRiskContext(
            account_equity=100_000.0,
            current_exposure_fraction=0.10,
            max_total_exposure_fraction=0.50,
        ),
        max_risk_fraction=0.02,
        required_agreeing_specialists=2,
        reject_conflicting_signals=True,
        specialist_signals=(
            SpecialistSignal(
                agent_id="chartist_technician",
                direction=TradeDirection.LONG,
                confidence=0.8,
            ),
            SpecialistSignal(
                agent_id="oracle_quant",
                direction=TradeDirection.LONG,
                confidence=0.7,
            ),
        ),
        news_risk_assessed=True,
    )


def _assembled():
    _, _, snapshot, run = _run()
    service = ProposalAssemblyService()
    proposal_input = _input(run.master_thesis)
    proposal = service.assemble(
        master_thesis=run.master_thesis,
        snapshot=snapshot,
        analysis_id=run.analysis.analysis_id,
        proposal_input=proposal_input,
    )
    return snapshot, run, service, proposal_input, proposal


def test_explicit_input_is_frozen_nested_and_requires_complete_explicit_values() -> None:
    snapshot, run, _, proposal_input, _ = _assembled()

    with pytest.raises(ValidationError):
        proposal_input.direction = TradeDirection.SHORT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        proposal_input.master_thesis_reference.thesis_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        proposal_input.risk_reward.ratio = 3.0  # type: ignore[misc]

    payload = proposal_input.model_dump()
    del payload["direction"]
    with pytest.raises(ValidationError):
        ExplicitTradeProposalInput.model_validate(payload)
    with pytest.raises(ValidationError, match="position_sizing_intent"):
        _input(run.master_thesis, position_sizing_intent=PositionSizingIntent.UNSPECIFIED)
    invalid_copy = proposal_input.model_copy(
        update={"position_sizing_intent": PositionSizingIntent.UNSPECIFIED}
    )
    with pytest.raises(ValidationError, match="position_sizing_intent"):
        ProposalAssemblyService().assemble(
            master_thesis=run.master_thesis,
            snapshot=snapshot,
            analysis_id=run.analysis.analysis_id,
            proposal_input=invalid_copy,
        )


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        (
            "reference",
            "Explicit input must reference the exact Master thesis.",
        ),
        ("analysis", "Master thesis must match the captured analysis ID."),
        ("evidence", "Master thesis must match the captured evidence snapshot."),
        ("symbol", "Master thesis symbol must exactly match the captured snapshot."),
        ("snapshot", "Master thesis must match the captured evidence snapshot."),
    ],
)
def test_assembly_rejects_any_thesis_or_snapshot_lineage_mismatch(
    replacement: str,
    expected: str,
) -> None:
    _, _, snapshot, run = _run()
    service = ProposalAssemblyService()
    thesis = run.master_thesis
    proposal_input = _input(thesis)
    analysis_id = run.analysis.analysis_id

    if replacement == "reference":
        proposal_input = _input(
            thesis,
            master_thesis_reference=MasterTradeThesisReference(
                thesis_id=thesis.thesis_id,
                fingerprint="f" * 64,
            ),
        )
    elif replacement == "analysis":
        analysis_id = "different-analysis"
    elif replacement == "evidence":
        thesis = thesis.model_copy(update={"evidence_fingerprint": "e" * 64})
        proposal_input = _input(thesis)
    elif replacement == "symbol":
        snapshot = replace(snapshot, symbol="EURUSD")
    else:
        other_store = EvidenceStore()
        other_store.ingest_market_event(_market_event(event_id="different-event", price=101.0))
        snapshot = other_store.snapshot(symbol="XAUUSD")

    with pytest.raises(ProposalAssemblyError, match=expected):
        service.assemble(
            master_thesis=thesis,
            snapshot=snapshot,
            analysis_id=analysis_id,
            proposal_input=proposal_input,
        )


def test_identical_explicit_input_has_deterministic_proposal_identity_and_fingerprint() -> None:
    _, _, snapshot, run = _run()
    service = ProposalAssemblyService()
    first_input = _input(run.master_thesis)
    second_input = _input(run.master_thesis)
    first = service.assemble(
        master_thesis=run.master_thesis,
        snapshot=snapshot,
        analysis_id=run.analysis.analysis_id,
        proposal_input=first_input,
    )
    second = service.assemble(
        master_thesis=run.master_thesis,
        snapshot=snapshot,
        analysis_id=run.analysis.analysis_id,
        proposal_input=second_input,
    )

    assert first == second
    assert proposal_fingerprint(first) == proposal_fingerprint(second)
    assert first.master_thesis_reference == run.master_thesis.as_reference()
    assert "must_not_reach_proposal" not in repr(first)


@pytest.mark.parametrize(
    "updates",
    [
        {"direction": TradeDirection.SHORT},
        {"reference_price": 101.0},
        {"stop_loss": 94.0},
        {"targets": (111.0,)},
        {"risk_reward": RiskReward(risk_distance=6.0, reward_distance=10.0, ratio=1.6666666667)},
        {"position_sizing_intent": PositionSizingIntent.FIXED_UNITS},
        {"requested_risk_fraction": 0.015},
    ],
)
def test_each_explicit_trade_parameter_change_changes_identity_and_fingerprint(
    updates: dict[str, object],
) -> None:
    snapshot, run, service, proposal_input, first = _assembled()
    changed_input = proposal_input.model_copy(update=updates)
    changed = service.assemble(
        master_thesis=run.master_thesis,
        snapshot=snapshot,
        analysis_id=run.analysis.analysis_id,
        proposal_input=changed_input,
    )

    assert changed.proposal_id != first.proposal_id
    assert proposal_fingerprint(changed) != proposal_fingerprint(first)


def test_explicit_input_not_thesis_bias_controls_direction_and_no_parameters_are_inferred() -> None:
    snapshot, run, service, _, _ = _assembled()
    explicit_short = _input(run.master_thesis, direction=TradeDirection.SHORT)
    proposal = service.assemble(
        master_thesis=run.master_thesis,
        snapshot=snapshot,
        analysis_id=run.analysis.analysis_id,
        proposal_input=explicit_short,
    )

    assert proposal.parameters == explicit_short.as_parameters()
    assert proposal.parameters.direction is TradeDirection.SHORT
    assert proposal.parameters.reference_price == explicit_short.reference_price
    assert proposal.parameters.stop_loss == explicit_short.stop_loss
    assert proposal.parameters.targets == explicit_short.targets
    assert proposal.parameters.risk_reward == explicit_short.risk_reward
    assert not hasattr(run.master_thesis, "direction")


def test_explicit_core_workflow_sends_only_the_final_assembled_proposal_to_sentinel() -> None:
    core, request, _, run = _run()
    result = core.run_explicit_trade_proposal_workflow(
        request,
        proposal_input=_input(run.master_thesis),
        risk_context=_risk_context(),
    )

    assert result.proposal.parameters is not None
    assert result.proposal.parameters.direction is TradeDirection.LONG
    assert result.sentinel_decision.proposal_id == result.proposal.proposal_id
    assert result.sentinel_decision.proposal_fingerprint == proposal_fingerprint(result.proposal)
    assert result.sentinel_decision.decision is SentinelDecisionStatus.APPROVED


def test_sentinel_rejects_snapshot_detached_proposal_before_registering_approval() -> None:
    snapshot, _, _, _, proposal = _assembled()
    other_store = EvidenceStore()
    other_store.ingest_market_event(_market_event(event_id="other-snapshot", price=101.0))
    detached_snapshot = other_store.snapshot(symbol="XAUUSD")
    decision = SentinelRiskGate().evaluate(
        proposal=proposal,
        snapshot=detached_snapshot,
        risk_context=_risk_context(),
    )

    assert snapshot != detached_snapshot
    assert decision.decision is SentinelDecisionStatus.REJECTED
    assert "proposal_evidence_fingerprint_does_not_match_snapshot" in decision.reasons
    assert decision.approval_id is None


def test_altered_proposal_cannot_claim_older_approval_and_pilot_stays_disabled() -> None:
    snapshot, _, _, _, proposal = _assembled()
    gate = SentinelRiskGate()
    approval = gate.evaluate(proposal=proposal, snapshot=snapshot, risk_context=_risk_context())
    altered = proposal.model_copy(
        update={"parameters": proposal.parameters.model_copy(update={"stop_loss": 94.0})}
    )
    pilot = PilotExecutionCoordinator(gate.registry)

    assert approval.decision is SentinelDecisionStatus.APPROVED
    with pytest.raises(PilotApprovalError, match="fingerprint"):
        pilot.prepare(proposal=altered, decision=approval, now=MOMENT + timedelta(minutes=2))
    disabled = pilot.prepare(proposal=proposal, decision=approval, now=MOMENT + timedelta(minutes=2))
    assert disabled.status == "DISABLED_NO_EXECUTION"


def test_thesis_and_assembly_service_cannot_mint_sentinel_approval_or_change_public_analyze() -> None:
    core, request, _, run = _run()
    service = ProposalAssemblyService()
    proposal = service.assemble(
        master_thesis=run.master_thesis,
        snapshot=core.evidence_snapshot(symbol="XAUUSD"),
        analysis_id=run.analysis.analysis_id,
        proposal_input=_input(run.master_thesis),
    )
    public_response = core.analyze(request)

    assert not hasattr(run.master_thesis, "approve")
    assert not hasattr(service, "approve")
    assert not hasattr(service, "register_approval")
    assert proposal.master_thesis_reference is not None
    assert "proposal" not in public_response.model_dump()
