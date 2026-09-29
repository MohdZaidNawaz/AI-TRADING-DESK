from hashlib import sha256
from datetime import datetime

from app.agents.base import AnalysisContext
from app.evidence.models import EvidenceSnapshot, canonical_symbol
from app.models.events import AssetClass
from app.models.proposals import (
    EvidenceReference,
    ObservedMarketReference,
    ProposedTradeParameters,
    SpecialistAnalysisReference,
    TradeProposal,
)
from app.models.schemas import AgentAnalysis, Synthesis
from app.models.specialist_analysis import (
    MasterSpecialistSynthesis,
    SpecialistAnalysis,
    summarize_specialist_analyses,
)


class MasterAgent:
    """Deterministic synthesis layer; replaceable by a model-backed adapter later."""

    agent_id = "master"
    name = "F.R.I.D.A.Y. — The Master"

    def synthesize(
        self,
        context: AnalysisContext,
        completed_results: list[AgentAnalysis],
    ) -> Synthesis:
        consulted = [result.agent_id for result in completed_results]
        agent_names = ", ".join(result.agent_name for result in completed_results)
        result_digest = " ".join(result.summary for result in completed_results)

        return Synthesis(
            status="completed",
            agents_consulted=consulted,
            summary=f"F.R.I.D.A.Y. — The Master routed {context.symbol} to {agent_names}.",
            final_response=(
                f"Deterministic mock synthesis for {context.symbol}: {result_digest} "
                "Treat these outputs as a research checklist only. Confirm all conclusions with verified "
                "market, news, and portfolio data before making any decision."
            ),
            caveat=(
                "No live data, AI model, broker, MT5 connection, trading signal, order placement, "
                "or execution capability was used."
            ),
        )

    def synthesize_specialist_analyses(
        self,
        *,
        analysis_id: str,
        evidence_fingerprint: str,
        specialist_analyses: tuple[SpecialistAnalysis, ...],
    ) -> MasterSpecialistSynthesis:
        """Create an internal evidence-linked synthesis without trade inference."""
        return summarize_specialist_analyses(
            parent_analysis_id=analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            analyses=specialist_analyses,
        )

    def create_draft_proposal(
        self,
        *,
        analysis_id: str,
        symbol: str | None,
        snapshot: EvidenceSnapshot,
        evidence_references: tuple[EvidenceReference, ...],
        evidence_fingerprint: str,
        created_at: datetime,
        specialist_results: list[AgentAnalysis],
        synthesis: Synthesis,
        specialist_analyses: tuple[SpecialistAnalysis, ...] = (),
        master_synthesis: MasterSpecialistSynthesis | None = None,
    ) -> TradeProposal:
        """Create a non-executable research draft without inventing trade inputs.

        Direction, entry, stop, targets, risk/reward, and sizing stay absent until
        a future structured capability explicitly supplies them.  The Master does
        not evaluate, approve, reject, or dispatch this draft.
        """
        canonical = canonical_symbol(symbol)
        observed_market_reference = None
        asset_class = AssetClass.UNKNOWN
        for evidence in snapshot.market_evidence:
            observed_price = evidence.price if evidence.price is not None else evidence.close
            if observed_price is None:
                continue
            matching_reference = next(
                (
                    reference
                    for reference in evidence_references
                    if reference.event_id == evidence.provenance.event_id
                    and reference.source_id == evidence.provenance.source_id
                ),
                None,
            )
            if matching_reference is not None:
                observed_market_reference = ObservedMarketReference(
                    evidence=matching_reference,
                    price=observed_price,
                    observed_at=evidence.timestamp,
                )
                asset_class = evidence.asset_class
                break

        if master_synthesis is not None and (
            master_synthesis.analysis_id != analysis_id
            or master_synthesis.evidence_fingerprint != evidence_fingerprint
        ):
            raise ValueError("Master synthesis must reference the proposal analysis snapshot")
        if master_synthesis is not None and tuple(
            artifact.analysis_id for artifact in master_synthesis.specialist_analyses
        ) != tuple(
            artifact.analysis_id
            for artifact in sorted(
                specialist_analyses,
                key=lambda item: (item.agent_id, item.analysis_id),
            )
        ):
            raise ValueError("Master synthesis must preserve the supplied specialist analyses")

        analyses_by_agent = {analysis.agent_id: analysis for analysis in specialist_analyses}
        specialist_references = tuple(
            SpecialistAnalysisReference(
                agent_id=result.agent_id,
                status=result.status,
                confidence=result.confidence,
                summary_digest=sha256(
                    f"{result.agent_id}\n{result.status}\n{result.summary}".encode("utf-8")
                ).hexdigest(),
                specialist_analysis_id=(
                    analyses_by_agent[result.agent_id].analysis_id
                    if result.agent_id in analyses_by_agent
                    else None
                ),
                specialist_analysis_fingerprint=(
                    analyses_by_agent[result.agent_id].analysis_fingerprint
                    if result.agent_id in analyses_by_agent
                    else None
                ),
                stance=(
                    analyses_by_agent[result.agent_id].stance
                    if result.agent_id in analyses_by_agent
                    else None
                ),
            )
            for result in specialist_results
        )
        missing = [
            "direction",
            "reference_price",
            "stop_loss",
            "target",
            "risk_reward",
            "position_sizing_intent",
            "requested_risk_fraction",
        ]
        if canonical is None:
            missing.insert(0, "symbol")

        proposal_id = sha256(
            f"{analysis_id}\n{evidence_fingerprint}\n{canonical or ''}".encode("utf-8")
        ).hexdigest()
        return TradeProposal(
            proposal_id=proposal_id,
            analysis_id=analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            evidence_references=evidence_references,
            specialist_references=specialist_references,
            symbol=canonical,
            asset_class=asset_class,
            observed_market_reference=observed_market_reference,
            parameters=ProposedTradeParameters(),
            thesis=(
                f"{synthesis.summary} {synthesis.caveat} "
                "This is a non-executable research draft; no trade parameters were inferred."
            ),
            created_at=created_at,
            missing_requirements=tuple(missing),
        )
