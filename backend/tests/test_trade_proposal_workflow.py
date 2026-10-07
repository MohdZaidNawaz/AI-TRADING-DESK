"""Deterministic tests for the Phase 3 non-executable proposal risk gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agents import create_friday_core
from app.agents.master import MasterAgent
from app.evidence import EvidenceStore
from app.execution import PilotApprovalError, PilotExecutionCoordinator
from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    NormalizedNewsResearchEvent,
    SourceMetadata,
    SourceTransport,
)
from app.models.proposals import (
    ExecutionEligibility,
    PortfolioRiskContext,
    PositionSizingIntent,
    ProposedTradeParameters,
    RiskContext,
    RiskReward,
    SentinelDecisionStatus,
    SpecialistSignal,
    TradeDirection,
    TradeProposal,
    TradeProposalStatus,
)
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
)
from app.models.schemas import AnalyzeRequest
from app.services.trade_proposal_workflow import (
    ApprovalRegistry,
    SentinelRiskGate,
    build_evidence_bundle,
    transition_proposal,
)


MARKET_SOURCE = SourceMetadata(
    source_id="phase3_test_market",
    display_name="Phase 3 Test Market",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)
NEWS_SOURCE = SourceMetadata(
    source_id="phase3_test_news",
    display_name="Phase 3 Test News",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.FINANCIAL_NEWS,
)
MOMENT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _market_event(
    *,
    event_id: str = "phase3-market-1",
    timestamp: datetime = MOMENT,
) -> NormalizedDataEvent:
    return NormalizedDataEvent(
        event_id=event_id,
        source=MARKET_SOURCE,
        event_type=EventType.MARKET_QUOTE,
        event="market_snapshot",
        asset_class=AssetClass.COMMODITY,
        symbol="XAUUSD",
        price=100.0,
        close=100.0,
        timestamp=timestamp,
        raw_payload={"must_not_reach_proposal": "raw"},
    )


def _market_bar(index: int, close: float) -> NormalizedMarketBar:
    opened_at = MOMENT + timedelta(hours=index)
    return NormalizedMarketBar(
        source=MarketSeriesSource(
            source_id="phase5b-risk-test",
            display_name="Phase 5B Risk Test",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        ),
        symbol="XAUUSD",
        asset_class=AssetClass.COMMODITY,
        venue="TEST",
        timeframe="1h",
        opened_at=opened_at,
        closed_at=opened_at + timedelta(hours=1),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100,
        volume_kind=VolumeKind.TICK,
        state=MarketBarState.COMPLETED,
        received_at=opened_at + timedelta(hours=1, seconds=1),
    )


def _snapshot(*, timestamp: datetime = MOMENT):
    store = EvidenceStore()
    store.ingest_market_event(_market_event(timestamp=timestamp))
    return store.snapshot(symbol="XAU/USD")


def _snapshot_with_news():
    store = EvidenceStore()
    store.ingest_market_event(_market_event())
    store.ingest_news_event(
        NormalizedNewsResearchEvent(
            event_id="phase3-news-1",
            source=NEWS_SOURCE,
            provider="Phase 3 Test News",
            title="Gold policy context",
            url="https://example.test/phase3-news",
            timestamp=MOMENT,
            symbols=["XAUUSD"],
            summary="A short normalized news summary.",
        )
    )
    return store.snapshot(symbol="XAUUSD")


def _parameters(
    *,
    direction: TradeDirection | None = TradeDirection.LONG,
    reference_price: float | None = 100.0,
    stop_loss: float | None = 95.0,
    targets: tuple[float, ...] = (110.0,),
    risk_reward: RiskReward | None = RiskReward(
        risk_distance=5.0,
        reward_distance=10.0,
        ratio=2.0,
    ),
    requested_risk_fraction: float | None = 0.01,
) -> ProposedTradeParameters:
    return ProposedTradeParameters(
        direction=direction,
        reference_price=reference_price,
        stop_loss=stop_loss,
        targets=targets,
        risk_reward=risk_reward,
        position_sizing_intent=PositionSizingIntent.RISK_FRACTION,
        requested_risk_fraction=requested_risk_fraction,
        quantity=1.0,
        order_type="MARKET",
        filling_mode="FOK",
    )


def _risk_context(
    *,
    as_of: datetime = MOMENT + timedelta(minutes=1),
    portfolio: PortfolioRiskContext | None = PortfolioRiskContext(
        account_equity=100_000.0,
        current_exposure_fraction=0.10,
        max_total_exposure_fraction=0.50,
    ),
    max_risk_fraction: float | None = 0.02,
    signals: tuple[SpecialistSignal, ...] = (
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
    required_agreeing_specialists: int | None = 2,
    reject_conflicting_signals: bool | None = True,
    max_market_evidence_age_seconds: int | None = 600,
    approval_ttl_seconds: int | None = 120,
    news_risk_assessed: bool | None = True,
) -> RiskContext:
    return RiskContext(
        as_of=as_of,
        max_market_evidence_age_seconds=max_market_evidence_age_seconds,
        approval_ttl_seconds=approval_ttl_seconds,
        portfolio=portfolio,
        max_risk_fraction=max_risk_fraction,
        required_agreeing_specialists=required_agreeing_specialists,
        reject_conflicting_signals=reject_conflicting_signals,
        specialist_signals=signals,
        news_risk_assessed=news_risk_assessed,
    )


def _proposal(
    *,
    snapshot=None,
    parameters: ProposedTradeParameters | None = None,
    symbol: str | None = "XAUUSD",
    status: TradeProposalStatus = TradeProposalStatus.PENDING_RISK,
) -> TradeProposal:
    snapshot = snapshot or _snapshot()
    bundle = build_evidence_bundle(snapshot)
    return TradeProposal(
        proposal_id="phase3-proposal",
        analysis_id="mock-phase3-analysis",
        evidence_fingerprint=bundle.fingerprint,
        evidence_references=bundle.references,
        symbol=symbol,
        asset_class=AssetClass.COMMODITY,
        parameters=parameters or _parameters(),
        thesis="Synthetic deterministic proposal for risk-gate tests.",
        created_at=bundle.created_at,
        status=status,
    )


def test_master_creates_a_non_executable_draft_without_inventing_trade_parameters() -> None:
    core = create_friday_core()

    result = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="Review technical momentum and risk")
    )

    assert result.proposal.status is TradeProposalStatus.INSUFFICIENT_DATA
    assert result.proposal.execution_eligibility is ExecutionEligibility.NOT_AUTHORIZED
    assert result.proposal.parameters is not None
    assert result.proposal.parameters.position_sizing_intent is PositionSizingIntent.UNSPECIFIED
    assert result.proposal.parameters.direction is None
    assert result.proposal.parameters.stop_loss is None
    assert result.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert result.risk_assessment is not None
    assert result.sentinel_decision.policy_id == result.risk_assessment.policy_id
    assert result.sentinel_decision.policy_fingerprint == result.risk_assessment.policy_fingerprint
    assert result.sentinel_decision.assessment_id == result.risk_assessment.assessment_id
    assert result.sentinel_decision.assessment_fingerprint == result.risk_assessment.fingerprint


def test_sentinel_decision_references_non_authorizing_policy_and_assessment() -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    decision, assessment = SentinelRiskGate().evaluate_with_assessment(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert decision.decision is SentinelDecisionStatus.APPROVED
    assert decision.policy_id == "sentinel-risk-policy"
    assert decision.policy_version == "phase7a-deterministic-v1"
    assert decision.policy_fingerprint == assessment.policy_fingerprint
    assert decision.assessment_id == assessment.assessment_id
    assert decision.assessment_fingerprint == assessment.fingerprint
    assert assessment.overall_status.value == "pass"
    assert any(
        item.reference_type == "legacy_risk_context"
        for check in assessment.checks
        for item in check.input_references
    )


def test_derived_features_do_not_create_trade_parameters_or_satisfy_sentinel() -> None:
    core = create_friday_core()
    core.ingest_market_bar(_market_bar(0, 100))
    core.ingest_market_bar(_market_bar(1, 110))

    result = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="Review technical returns and risk")
    )

    assert result.proposal.parameters is not None
    assert result.proposal.parameters.direction is None
    assert result.proposal.parameters.reference_price is None
    assert result.proposal.parameters.stop_loss is None
    assert result.proposal.parameters.targets == ()
    assert result.proposal.parameters.requested_risk_fraction is None
    assert all(
        reference.stance.value == "insufficient_data"
        for reference in result.proposal.specialist_references
    )
    assert result.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in result.sentinel_decision.missing_requirements


def test_complete_proposal_with_explicit_context_is_approved() -> None:
    snapshot = _snapshot()
    gate = SentinelRiskGate()

    decision = gate.evaluate(
        proposal=_proposal(snapshot=snapshot),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert decision.decision is SentinelDecisionStatus.APPROVED
    assert decision.approval_id is not None
    assert decision.expires_at == MOMENT + timedelta(minutes=3)


def test_proposal_links_only_to_safe_evidence_provenance() -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)

    assert proposal.evidence_references[0].event_id == "phase3-market-1"
    assert proposal.evidence_references[0].source_id == "phase3_test_market"
    assert "must_not_reach_proposal" not in repr(proposal)
    assert "raw" not in repr(proposal)


def test_evidence_and_proposal_fingerprints_are_deterministic() -> None:
    first_snapshot = _snapshot()
    second_snapshot = _snapshot()

    first = _proposal(snapshot=first_snapshot)
    second = _proposal(snapshot=second_snapshot)

    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert first.proposal_id == second.proposal_id


@pytest.mark.parametrize(
    ("parameters", "required"),
    [
        (_parameters(direction=None), "direction"),
        (_parameters(stop_loss=None), "stop_loss"),
        (_parameters(targets=()), "target"),
        (_parameters(risk_reward=None), "risk_reward"),
    ],
)
def test_missing_trade_parameters_return_insufficient_data(
    parameters: ProposedTradeParameters,
    required: str,
) -> None:
    snapshot = _snapshot()
    decision = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot, parameters=parameters),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert required in decision.missing_requirements
    assert decision.approval_id is None


def test_missing_symbol_returns_insufficient_data() -> None:
    snapshot = _snapshot()
    decision = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot, symbol=None),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "symbol" in decision.missing_requirements


def test_draft_or_expired_proposal_cannot_bypass_the_pending_risk_lifecycle() -> None:
    snapshot = _snapshot()
    draft = _proposal(snapshot=snapshot, status=TradeProposalStatus.DRAFT)
    expired = _proposal(snapshot=snapshot).model_copy(update={"expires_at": MOMENT})
    gate = SentinelRiskGate()

    draft_decision = gate.evaluate(
        proposal=draft,
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    expired_decision = gate.evaluate(
        proposal=expired,
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert draft_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "proposal_not_pending_risk" in draft_decision.missing_requirements
    assert expired_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "proposal_expired" in expired_decision.missing_requirements
    assert transition_proposal(expired, expired_decision).status is TradeProposalStatus.EXPIRED


def test_invalid_stop_direction_is_rejected() -> None:
    snapshot = _snapshot()
    decision = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot, parameters=_parameters(stop_loss=101.0)),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert decision.decision is SentinelDecisionStatus.REJECTED
    assert "stop_loss_not_below_long_reference_price" in decision.reasons


def test_invalid_non_positive_risk_reward_is_rejected() -> None:
    snapshot = _snapshot()
    decision = SentinelRiskGate().evaluate(
        proposal=_proposal(
            snapshot=snapshot,
            parameters=_parameters(
                risk_reward=RiskReward(risk_distance=5.0, reward_distance=0.0, ratio=0.0)
            ),
        ),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert decision.decision is SentinelDecisionStatus.REJECTED
    assert "invalid_non_positive_risk_reward" in decision.reasons


def test_missing_or_stale_market_evidence_returns_insufficient_data() -> None:
    fresh_snapshot = _snapshot()
    stale_snapshot = _snapshot(timestamp=MOMENT)
    missing_market_snapshot = EvidenceStore().snapshot(symbol="XAUUSD")

    stale = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=stale_snapshot),
        snapshot=stale_snapshot,
        risk_context=_risk_context(as_of=MOMENT + timedelta(minutes=20)),
    )
    missing = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=missing_market_snapshot),
        snapshot=missing_market_snapshot,
        risk_context=_risk_context(),
    )

    assert stale.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "fresh_market_evidence" in stale.missing_requirements
    assert missing.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in missing.missing_requirements


def test_absent_portfolio_or_sizing_context_returns_insufficient_data() -> None:
    snapshot = _snapshot()
    missing_portfolio = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot),
        snapshot=snapshot,
        risk_context=_risk_context(portfolio=None),
    )
    missing_limit = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot),
        snapshot=snapshot,
        risk_context=_risk_context(max_risk_fraction=None),
    )

    assert missing_portfolio.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "portfolio_context" in missing_portfolio.missing_requirements
    assert missing_limit.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "sizing_limit" in missing_limit.missing_requirements


def test_unassessed_news_risk_returns_insufficient_data() -> None:
    snapshot = _snapshot_with_news()
    decision = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot),
        snapshot=snapshot,
        risk_context=_risk_context(news_risk_assessed=False),
    )

    assert decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "news_risk_assessment" in decision.missing_requirements


def test_explicit_risk_limit_violation_and_conflicting_signals_are_rejected() -> None:
    snapshot = _snapshot()
    risk_violation = SentinelRiskGate().evaluate(
        proposal=_proposal(
            snapshot=snapshot,
            parameters=_parameters(requested_risk_fraction=0.03),
        ),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    conflict = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot),
        snapshot=snapshot,
        risk_context=_risk_context(
            signals=(
                SpecialistSignal(
                    agent_id="chartist_technician",
                    direction=TradeDirection.LONG,
                    confidence=0.8,
                ),
                SpecialistSignal(
                    agent_id="oracle_quant",
                    direction=TradeDirection.SHORT,
                    confidence=0.7,
                ),
            )
        ),
    )

    assert risk_violation.decision is SentinelDecisionStatus.REJECTED
    assert "requested_risk_exceeds_configured_limit" in risk_violation.reasons
    assert conflict.decision is SentinelDecisionStatus.REJECTED
    assert "conflicting_structured_specialist_signals" in conflict.reasons


def test_rejected_and_insufficient_proposals_cannot_produce_approvals() -> None:
    snapshot = _snapshot()
    rejected = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot, parameters=_parameters(stop_loss=101.0)),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    insufficient = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot, parameters=_parameters(direction=None)),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )

    assert rejected.approval_id is None
    assert insufficient.approval_id is None


def test_identical_approval_is_idempotent_and_single_use_for_pilot() -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    gate = SentinelRiskGate()
    first = gate.evaluate(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    second = gate.evaluate(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    pilot = PilotExecutionCoordinator(gate.registry)

    assert first == second
    disabled = pilot.prepare(
        proposal=proposal,
        decision=first,
        now=MOMENT + timedelta(minutes=2),
    )
    assert disabled.status == "DISABLED_NO_EXECUTION"
    with pytest.raises(PilotApprovalError, match="already been consumed"):
        pilot.prepare(
            proposal=proposal,
            decision=first,
            now=MOMENT + timedelta(minutes=2),
        )


def test_pilot_rejects_missing_rejected_mismatched_and_expired_approvals() -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    gate = SentinelRiskGate(ApprovalRegistry())
    approved = gate.evaluate(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    pilot = PilotExecutionCoordinator(gate.registry)
    rejected = SentinelRiskGate().evaluate(
        proposal=_proposal(snapshot=snapshot, parameters=_parameters(stop_loss=101.0)),
        snapshot=snapshot,
        risk_context=_risk_context(),
    )
    mismatched = _proposal(snapshot=snapshot).model_copy(update={"proposal_id": "other-proposal"})

    with pytest.raises(PilotApprovalError, match="required"):
        pilot.prepare(proposal=proposal, decision=rejected, now=MOMENT + timedelta(minutes=1))
    with pytest.raises(PilotApprovalError, match="proposal ID"):
        pilot.prepare(proposal=mismatched, decision=approved, now=MOMENT + timedelta(minutes=1))
    with pytest.raises(PilotApprovalError, match="expired"):
        pilot.prepare(proposal=proposal, decision=approved, now=MOMENT + timedelta(minutes=3))


def test_internal_workflow_is_deterministic_and_public_analyze_shape_is_unchanged() -> None:
    core = create_friday_core()
    core.ingest_event(_market_event())
    request = AnalyzeRequest(symbol="XAUUSD", query="Review technical momentum and risk")

    first = core.run_trade_proposal_workflow(request)
    second = core.run_trade_proposal_workflow(request)
    public_response = core.analyze(request)

    assert first == second
    assert first.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "proposal" not in public_response.model_dump()
    assert public_response.mode == "deterministic_mock"


def test_master_cannot_approve_and_sentinel_service_cannot_invoke_pilot() -> None:
    master_source = Path(MasterAgent.__module__.replace(".", "/") + ".py")
    # Resolve source paths from this repository rather than relying on a live service.
    backend_root = Path(__file__).resolve().parents[1]
    master_text = (backend_root / "app/agents/master.py").read_text(encoding="utf-8").lower()
    sentinel_text = (backend_root / "app/services/trade_proposal_workflow.py").read_text(
        encoding="utf-8"
    ).lower()

    assert not hasattr(MasterAgent, "approve")
    assert not hasattr(MasterAgent, "reject")
    assert "app.execution" not in sentinel_text
    assert "pilotexecutioncoordinator" not in sentinel_text
    assert master_source.name == "master.py"


def test_phase_three_files_contain_no_execution_adapter_imports_or_calls() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    paths = (
        backend_root / "app/models/proposals.py",
        backend_root / "app/services/trade_proposal_workflow.py",
        backend_root / "app/execution/pilot.py",
    )
    forbidden = (
        "import mt5",
        "from mt5",
        "import metatrader",
        "from metatrader",
        "place_order(",
        "execute_trade(",
        "broker.connect(",
    )

    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(value in text for value in forbidden)
