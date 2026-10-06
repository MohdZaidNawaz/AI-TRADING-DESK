"""Phase 6A-1 tests for the immutable non-authorizing Master thesis."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agents import create_friday_core
from app.agents.base import AnalysisLifecycle
from app.agents.master import MasterAgent
from app.evidence.models import EvidenceSnapshot
from app.models.events import DataDomain
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import (
    SpecialistAnalysisStatus,
    SpecialistEvidenceReference,
    SpecialistStance,
    create_specialist_analysis,
    summarize_specialist_analyses,
)
from app.models.trade_thesis import (
    ThesisAnalyticalBias,
    ThesisAuthorizationStatus,
)
from app.services.master_decision import MasterDecisionService


MOMENT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
EVIDENCE_FINGERPRINT = "a" * 64


def _artifact(agent_id: str, stance: SpecialistStance):
    return create_specialist_analysis(
        parent_analysis_id="analysis-1",
        agent_id=agent_id,
        status=SpecialistAnalysisStatus.COMPLETED,
        created_at=MOMENT,
        evidence_fingerprint=EVIDENCE_FINGERPRINT,
        confidence=0.0,
        stance=stance,
        thesis=f"{agent_id} analytical artifact.",
        supporting_evidence=(
            SpecialistEvidenceReference(
                event_id="evidence-1",
                source_id="synthetic-source",
                domain=DataDomain.MARKET_DATA,
                timestamp=MOMENT,
                symbol="XAUUSD",
            ),
        ),
        limitations=("Synthetic deterministic specialist artifact.",),
        invalidating_conditions=("Requires independent validation.",),
    )


def _thesis(*stances: SpecialistStance):
    analyses = tuple(_artifact(f"agent-{index}", stance) for index, stance in enumerate(stances))
    synthesis = summarize_specialist_analyses(
        parent_analysis_id="analysis-1",
        evidence_fingerprint=EVIDENCE_FINGERPRINT,
        analyses=analyses,
    )
    return MasterDecisionService().build(
        analysis_id="analysis-1",
        symbol="XAU/USD",
        evidence_fingerprint=EVIDENCE_FINGERPRINT,
        evidence_as_of=MOMENT,
        master_synthesis=synthesis,
    ), synthesis


def test_agreement_thesis_is_immutable_and_preserves_artifact_lineage() -> None:
    thesis, synthesis = _thesis(
        SpecialistStance.BULLISH,
        SpecialistStance.BULLISH,
        SpecialistStance.NEUTRAL,
    )
    assert thesis.analytical_bias is ThesisAnalyticalBias.BULLISH
    assert thesis.consensus_state.value == "agreement"
    assert thesis.authorization_status is ThesisAuthorizationStatus.NON_EXECUTABLE_NON_AUTHORIZING
    assert thesis.evidence_fingerprint == EVIDENCE_FINGERPRINT
    assert thesis.master_synthesis_id == synthesis.synthesis_id
    assert len(thesis.specialist_artifact_references) == 3
    assert thesis.evidence_coverage.referenced_evidence_count == 1
    with pytest.raises(ValidationError):
        thesis.symbol = "EURUSD"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        thesis.evidence_references[0].event_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="raw_payload"):
        type(thesis).model_validate({**thesis.model_dump(), "raw_payload": {"unsafe": True}})


def test_thesis_identity_is_deterministic_across_specialist_insertion_order() -> None:
    artifacts = (
        _artifact("chartist_technician", SpecialistStance.BEARISH),
        _artifact("oracle_quant", SpecialistStance.BEARISH),
    )
    first_synthesis = summarize_specialist_analyses(
        parent_analysis_id="analysis-1", evidence_fingerprint=EVIDENCE_FINGERPRINT, analyses=artifacts
    )
    second_synthesis = summarize_specialist_analyses(
        parent_analysis_id="analysis-1", evidence_fingerprint=EVIDENCE_FINGERPRINT, analyses=tuple(reversed(artifacts))
    )
    service = MasterDecisionService()
    first = service.build(
        analysis_id="analysis-1", symbol="XAU/USD", evidence_fingerprint=EVIDENCE_FINGERPRINT,
        evidence_as_of=MOMENT, master_synthesis=first_synthesis,
    )
    second = service.build(
        analysis_id="analysis-1", symbol="XAU/USD", evidence_fingerprint=EVIDENCE_FINGERPRINT,
        evidence_as_of=MOMENT, master_synthesis=second_synthesis,
    )
    assert first.thesis_id == second.thesis_id
    assert first.fingerprint == second.fingerprint
    assert first.analytical_bias is ThesisAnalyticalBias.BEARISH


def test_conflict_and_insufficient_data_preserve_existing_synthesis_semantics() -> None:
    conflicted, _ = _thesis(SpecialistStance.BULLISH, SpecialistStance.BEARISH)
    insufficient, _ = _thesis(*((SpecialistStance.INSUFFICIENT_DATA,) * 9))
    assert conflicted.analytical_bias is ThesisAnalyticalBias.CONFLICTED
    assert insufficient.analytical_bias is ThesisAnalyticalBias.INSUFFICIENT_DATA
    for thesis in (conflicted, insufficient):
        payload = thesis.model_dump(mode="json")
        assert "direction" not in payload
        assert not any(key in payload for key in ("entry", "stop_loss", "target", "sizing", "approval_id"))
        assert not hasattr(thesis, "direction")
        assert not hasattr(thesis, "parameters")
        assert not hasattr(thesis, "approval_id")
        assert not hasattr(thesis, "execution_instruction")


def test_orchestrator_creates_one_thesis_and_workflow_keeps_sentinel_requirements() -> None:
    core = create_friday_core()
    analysis_run = core._analyze_run(  # noqa: SLF001 - verifies one private run artifact.
        AnalyzeRequest(symbol="XAUUSD", query="Review market risk"),
        snapshot=EvidenceSnapshot(symbol="XAUUSD"),
        lifecycle=AnalysisLifecycle(),
    )
    thesis = analysis_run.master_thesis
    assert thesis.analysis_id == analysis_run.analysis.analysis_id
    assert thesis.master_synthesis_id == analysis_run.master_synthesis.synthesis_id
    assert thesis.analytical_bias is ThesisAnalyticalBias.INSUFFICIENT_DATA

    workflow = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="Review market risk")
    )
    assert workflow.proposal.master_thesis_reference is not None
    assert workflow.proposal.master_thesis_reference.thesis_id
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    assert "portfolio_context" in workflow.sentinel_decision.missing_requirements
    assert "sizing_limit" in workflow.sentinel_decision.missing_requirements
    assert "structured_specialist_agreement" in workflow.sentinel_decision.missing_requirements
    assert workflow.sentinel_decision.approval_id is None
