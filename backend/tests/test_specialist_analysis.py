"""Deterministic tests for Phase 4 structured specialist intelligence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents import create_friday_core
from app.agents.base import AnalysisLifecycle
from app.evidence import EvidenceStore
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
    PortfolioRiskContext,
    PositionSizingIntent,
    ProposedTradeParameters,
    RiskContext,
    RiskReward,
    SentinelDecisionStatus,
    SpecialistAnalysisReference,
    TradeDirection,
    TradeProposal,
    TradeProposalStatus,
)
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import (
    SpecialistAnalysis,
    SpecialistAnalysisStatus,
    SpecialistConsensus,
    SpecialistEvidenceReference,
    SpecialistFactObservation,
    SpecialistOperationalState,
    SpecialistStance,
    create_specialist_analysis,
    summarize_specialist_analyses,
)
from app.services.trade_proposal_workflow import SentinelRiskGate, build_evidence_bundle


MOMENT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
MARKET_SOURCE = SourceMetadata(
    source_id="phase4-test-market",
    display_name="Phase 4 Test Market",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)
NEWS_SOURCE = SourceMetadata(
    source_id="phase4-test-news",
    display_name="Phase 4 Test News",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.FINANCIAL_NEWS,
)


def _market_event() -> NormalizedDataEvent:
    return NormalizedDataEvent(
        event_id="phase4-market-1",
        source=MARKET_SOURCE,
        event_type=EventType.MARKET_CANDLE,
        event="market_snapshot",
        asset_class=AssetClass.COMMODITY,
        symbol="XAUUSD",
        timeframe="60",
        price=100.0,
        open=98.0,
        high=101.0,
        low=97.0,
        close=100.0,
        volume=1_000.0,
        indicator_values={"provider_neutral_value": 7.0},
        timestamp=MOMENT,
        raw_payload={"must_not_reach_specialists": "raw-payload"},
    )


def _news_event() -> NormalizedNewsResearchEvent:
    return NormalizedNewsResearchEvent(
        event_id="phase4-news-1",
        source=NEWS_SOURCE,
        provider="Phase 4 Test News",
        title="Normalized gold market context",
        url="https://example.test/phase4-news",
        timestamp=MOMENT,
        symbols=["XAUUSD"],
        topics=["commodities"],
        summary="A short normalized summary.",
        content="This article body must never reach a specialist artifact.",
    )


def _snapshot():
    store = EvidenceStore()
    store.ingest_market_event(_market_event())
    store.ingest_news_event(_news_event())
    return store.snapshot(symbol="XAU/USD")


def _reference() -> SpecialistEvidenceReference:
    return SpecialistEvidenceReference(
        event_id="phase4-market-1",
        source_id="phase4-test-market",
        domain=DataDomain.MARKET_DATA,
        timestamp=MOMENT,
        symbol="XAUUSD",
    )


def _analysis(
    *,
    agent_id: str = "chartist_technician",
    stance: SpecialistStance = SpecialistStance.INSUFFICIENT_DATA,
    confidence: float = 0.0,
    supporting_evidence: tuple[SpecialistEvidenceReference, ...] | None = None,
) -> SpecialistAnalysis:
    references = supporting_evidence if supporting_evidence is not None else (_reference(),)
    facts = (
        SpecialistFactObservation(
            statement="Observed a normalized market value.",
            evidence_references=references,
        ),
    )
    return create_specialist_analysis(
        parent_analysis_id="mock-phase4-analysis",
        agent_id=agent_id,
        status=SpecialistAnalysisStatus.COMPLETED,
        created_at=MOMENT,
        evidence_fingerprint="a" * 64,
        confidence=confidence,
        stance=stance,
        thesis="A deterministic structured specialist thesis.",
        fact_observations=facts,
        supporting_evidence=references,
        operational_state=(
            SpecialistOperationalState.DISABLED_NO_EXECUTION
            if agent_id == "pilot_execution"
            else None
        ),
    )


def _analysis_run():
    core = create_friday_core()
    snapshot = _snapshot()
    return core._analyze_run(  # noqa: SLF001 - verifies the private Phase 4 boundary.
        AnalyzeRequest(symbol="XAUUSD", query="Give me a complete overview"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )


def test_specialist_analysis_is_frozen_validated_and_deterministic() -> None:
    first = _analysis()
    second = _analysis()

    assert first == second
    assert first.analysis_id == second.analysis_id
    assert first.analysis_fingerprint == second.analysis_fingerprint
    with pytest.raises(ValidationError):
        first.thesis = "mutated"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _analysis(confidence=1.01)
    with pytest.raises(ValidationError):
        _analysis(confidence=float("nan"))


def test_fact_observations_require_assigned_safe_evidence_references() -> None:
    reference = _reference()
    with pytest.raises(ValidationError):
        SpecialistFactObservation(statement="Unsupported fact.", evidence_references=())

    with pytest.raises(ValidationError, match="assigned supporting evidence"):
        SpecialistAnalysis(
            analysis_id="specialist-test",
            analysis_fingerprint="b" * 64,
            agent_id="atlas_macro",
            status=SpecialistAnalysisStatus.COMPLETED,
            created_at=MOMENT,
            evidence_fingerprint="a" * 64,
            confidence=0.0,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis="Traceability test.",
            fact_observations=(
                SpecialistFactObservation(
                    statement="Observed normalized evidence.",
                    evidence_references=(reference,),
                ),
            ),
            supporting_evidence=(),
        )


def test_all_nine_specialists_produce_snapshot_linked_artifacts() -> None:
    run = _analysis_run()
    artifacts = run.specialist_analyses

    assert [artifact.agent_id for artifact in artifacts] == [
        "atlas_macro",
        "capitol_smart_money",
        "scout_recon",
        "athena_analyst",
        "chartist_technician",
        "oracle_quant",
        "sentinel_risk_officer",
        "pilot_execution",
        "ledger_the_book",
    ]
    assert len({artifact.analysis_id for artifact in artifacts}) == 9
    assert {artifact.evidence_fingerprint for artifact in artifacts} == {
        run.master_synthesis.evidence_fingerprint
    }
    assert {
        artifact.analysis_id for artifact in run.master_synthesis.specialist_analyses
    } == {artifact.analysis_id for artifact in artifacts}
    assert all(artifact.stance is SpecialistStance.INSUFFICIENT_DATA for artifact in artifacts)
    assert all(artifact.created_at == MOMENT for artifact in artifacts)


def test_specialist_evidence_routing_and_safe_content_are_preserved() -> None:
    artifacts = {artifact.agent_id: artifact for artifact in _analysis_run().specialist_analyses}
    chartist = artifacts["chartist_technician"]
    oracle = artifacts["oracle_quant"]
    scout = artifacts["scout_recon"]
    ledger = artifacts["ledger_the_book"]

    assert {reference.domain for reference in chartist.supporting_evidence} == {
        DataDomain.MARKET_DATA
    }
    assert {reference.domain for reference in oracle.supporting_evidence} == {
        DataDomain.MARKET_DATA
    }
    assert "open=98.0" in chartist.fact_observations[0].statement
    assert "indicator.provider_neutral_value=7.0" in oracle.fact_observations[0].statement
    assert any("Normalized gold market context" in fact.statement for fact in scout.fact_observations)
    assert ledger.lifecycle is not None
    rendered = repr(tuple(artifacts.values()))
    assert "raw-payload" not in rendered
    assert "article body" not in rendered


def test_unsupported_domains_and_pilot_remain_non_authorizing() -> None:
    artifacts = {artifact.agent_id: artifact for artifact in _analysis_run().specialist_analyses}

    assert artifacts["capitol_smart_money"].stance is SpecialistStance.INSUFFICIENT_DATA
    assert artifacts["athena_analyst"].stance is SpecialistStance.INSUFFICIENT_DATA
    assert artifacts["pilot_execution"].operational_state is (
        SpecialistOperationalState.DISABLED_NO_EXECUTION
    )
    assert "execution authority" in artifacts["pilot_execution"].thesis.lower()


def test_master_consensus_is_deterministic_and_distinguishes_conflict_from_insufficiency() -> None:
    bullish = _analysis(agent_id="chartist_technician", stance=SpecialistStance.BULLISH, confidence=0.8)
    confirming = _analysis(agent_id="oracle_quant", stance=SpecialistStance.BULLISH, confidence=0.7)
    bearish = _analysis(agent_id="atlas_macro", stance=SpecialistStance.BEARISH, confidence=0.6)

    agreement = summarize_specialist_analyses(
        parent_analysis_id="mock-phase4-analysis",
        evidence_fingerprint="a" * 64,
        analyses=(confirming, bullish),
    )
    agreement_reordered = summarize_specialist_analyses(
        parent_analysis_id="mock-phase4-analysis",
        evidence_fingerprint="a" * 64,
        analyses=(bullish, confirming),
    )
    conflict = summarize_specialist_analyses(
        parent_analysis_id="mock-phase4-analysis",
        evidence_fingerprint="a" * 64,
        analyses=(bearish, bullish),
    )
    insufficient = summarize_specialist_analyses(
        parent_analysis_id="mock-phase4-analysis",
        evidence_fingerprint="a" * 64,
        analyses=(_analysis(),),
    )

    assert agreement.consensus is SpecialistConsensus.AGREEMENT
    assert agreement == agreement_reordered
    assert conflict.consensus is SpecialistConsensus.CONFLICTED
    assert conflict.conflicting_analysis_ids == tuple(
        sorted((bearish.analysis_id, bullish.analysis_id))
    )
    assert insufficient.consensus is SpecialistConsensus.INSUFFICIENT_DATA


def test_proposal_retains_specialist_artifact_traceability_without_trade_inference() -> None:
    core = create_friday_core()
    core.ingest_event(_market_event())

    result = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="Give me a complete overview")
    )

    references = result.proposal.specialist_references
    assert len(references) == 9
    assert all(reference.specialist_analysis_id for reference in references)
    assert all(reference.specialist_analysis_fingerprint for reference in references)
    assert all(reference.stance is SpecialistStance.INSUFFICIENT_DATA for reference in references)
    assert result.proposal.parameters is not None
    assert result.proposal.parameters.direction is None
    assert result.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA


def test_structured_stance_cannot_satisfy_sentinel_policy_signals() -> None:
    snapshot = _snapshot()
    bundle = build_evidence_bundle(snapshot)
    proposal = TradeProposal(
        proposal_id="phase4-proposal",
        analysis_id="mock-phase4-analysis",
        evidence_fingerprint=bundle.fingerprint,
        evidence_references=bundle.references,
        specialist_references=(
            SpecialistAnalysisReference(
                agent_id="chartist_technician",
                status="completed",
                confidence=0.9,
                summary_digest="c" * 64,
                specialist_analysis_id="specialist-bullish",
                specialist_analysis_fingerprint="d" * 64,
                stance=SpecialistStance.BULLISH,
            ),
            SpecialistAnalysisReference(
                agent_id="oracle_quant",
                status="completed",
                confidence=0.8,
                summary_digest="e" * 64,
                specialist_analysis_id="specialist-confirming",
                specialist_analysis_fingerprint="f" * 64,
                stance=SpecialistStance.BULLISH,
            ),
        ),
        symbol="XAUUSD",
        asset_class=AssetClass.COMMODITY,
        parameters=ProposedTradeParameters(
            direction=TradeDirection.LONG,
            reference_price=100.0,
            stop_loss=95.0,
            targets=(110.0,),
            risk_reward=RiskReward(risk_distance=5.0, reward_distance=10.0, ratio=2.0),
            position_sizing_intent=PositionSizingIntent.RISK_FRACTION,
            requested_risk_fraction=0.01,
        ),
        thesis="Explicit non-executable test proposal.",
        created_at=bundle.created_at,
        status=TradeProposalStatus.PENDING_RISK,
    )
    risk_context = RiskContext(
        as_of=MOMENT,
        max_market_evidence_age_seconds=600,
        approval_ttl_seconds=60,
        portfolio=PortfolioRiskContext(
            account_equity=100_000.0,
            current_exposure_fraction=0.1,
            max_total_exposure_fraction=0.5,
        ),
        max_risk_fraction=0.02,
        required_agreeing_specialists=2,
        reject_conflicting_signals=True,
        specialist_signals=(),
        news_risk_assessed=True,
    )

    decision = SentinelRiskGate().evaluate(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=risk_context,
    )

    assert decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "structured_specialist_agreement" in decision.missing_requirements


def test_public_analysis_contract_stays_internal_and_phase_four_files_add_no_execution_imports() -> None:
    response = create_friday_core().analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Give me a complete overview")
    )

    assert "specialist_analyses" not in response.model_dump()
    assert response.model_dump().keys() == {
        "analysis_id",
        "mode",
        "symbol",
        "query",
        "selected_agents",
        "routing",
        "agent_results",
        "synthesis",
    }

    backend_root = Path(__file__).resolve().parents[1]
    paths = (
        backend_root / "app/models/specialist_analysis.py",
        backend_root / "app/agents/base.py",
        backend_root / "app/agents/specialists.py",
        backend_root / "app/agents/orchestrator.py",
        backend_root / "app/agents/master.py",
    )
    forbidden = (
        "import mt5",
        "from mt5",
        "import metatrader",
        "from metatrader",
        "broker.connect(",
        "place_order(",
        "execute_trade(",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(value in text for value in forbidden)
