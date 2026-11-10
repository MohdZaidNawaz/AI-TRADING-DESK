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
from app.models.derived_features import FeatureStatus
from app.models.technical_analysis import TrendDirection
from app.models.trade_thesis import MasterTradeThesis
from app.models.specialist_analysis import (
    MasterSpecialistSynthesis,
    SpecialistAnalysis,
    SpecialistConsensus,
    SpecialistStance,
    summarize_specialist_analyses,
)
from app.models.agent_coordination import AgentCoordinationReport, AgentEvidenceLink
from app.services.master_decision import MasterDecisionService


class MasterAgent:
    """Deterministic synthesis layer; replaceable by a model-backed adapter later."""

    agent_id = "master"
    name = "F.R.I.D.A.Y. — The Master"

    def __init__(self, *, decision_service: MasterDecisionService | None = None) -> None:
        self._decision_service = decision_service or MasterDecisionService()

    def synthesize(
        self,
        context: AnalysisContext,
        completed_results: list[AgentAnalysis],
        *,
        specialist_synthesis: MasterSpecialistSynthesis | None = None,
        coordination: AgentCoordinationReport | None = None,
    ) -> Synthesis:
        consulted = [result.agent_id for result in completed_results]
        agent_names = ", ".join(result.agent_name for result in completed_results)
        result_digest = " ".join(result.summary for result in completed_results)
        research = context.research_context
        intelligence = context.news_intelligence
        intelligence_summary = ""
        if intelligence is not None:
            news_count = len(intelligence.relevant_news)
            event_count = len(intelligence.upcoming_events) + len(intelligence.released_events)
            intelligence_summary = (
                f" News/economic evidence: {news_count} relevant article(s), {event_count} "
                f"calendar event(s), and {len(intelligence.macro_findings)} deterministic "
                "interpretation(s). "
                f"Provider status={intelligence.news_retrieval.status.value}; "
                f"cooldown={intelligence.news_retrieval.cooldown_status.value}; "
                f"cache={intelligence.news_retrieval.cache_status.value}. "
                "Verified facts remain distinct from possible implications."
            )
            if intelligence.missing_information:
                intelligence_summary += (
                    " Missing information: " + " ".join(intelligence.missing_information)
                )
            if intelligence.warnings:
                intelligence_summary += " Warnings: " + " ".join(intelligence.warnings)
        macro = context.macro_intelligence
        if macro is not None:
            usable_facts = sum(
                len(item.verified_facts) for item in macro.instrument_assessments
            )
            calculated_changes = sum(
                item.status.value == "available" for item in macro.indicator_changes
            )
            calculated_differentials = sum(
                item.status.value == "available" for item in macro.policy_rate_differentials
            )
            conflict_count = sum(
                len(item.conflicting_evidence_ids) for item in macro.instrument_assessments
            )
            provider_states = ", ".join(
                f"{item.provider_id or 'unconfigured'}={item.status.value}"
                for item in macro.provider_diagnostics
            )
            intelligence_summary += (
                f" Macro evidence: {usable_facts} verified cached fact(s); "
                f"{calculated_changes} period change(s); "
                f"{calculated_differentials} policy differential(s); "
                f"{conflict_count} conflicting evidence reference(s); "
                f"providers: {provider_states}. Macro facts and calculations remain separate "
                "from conditional implications, do not establish causation, and do not grant "
                "execution authority."
            )
        liquidity = context.liquidity_intelligence
        if liquidity is not None:
            level_count = sum(len(item.liquidity_levels) for item in liquidity.analyses)
            sweep_count = sum(len(item.sweeps) for item in liquidity.analyses)
            gap_count = sum(len(item.fair_value_gaps) for item in liquidity.analyses)
            break_count = sum(len(item.structure_breaks) for item in liquidity.analyses)
            block_count = sum(len(item.order_block_candidates) for item in liquidity.analyses)
            intelligence_summary += (
                f" Liquidity price-action evidence: {level_count} candidate level(s), "
                f"{sweep_count} sweep event(s), {gap_count} FVG(s), {break_count} "
                f"BOS/CHoCH event(s), and {block_count} order-block candidate(s); "
                f"alignment={liquidity.alignment.state.value}. These are deterministic OHLCV "
                "patterns, not observed institutional orders, intent, positioning, or a trade signal."
            )
        risk_strategy = context.risk_strategy_intelligence
        if risk_strategy is not None:
            scenario_states = ", ".join(
                f"{item.direction.value}={item.status.value}/quality-{item.quality.score:.1f}"
                for item in risk_strategy.scenarios
            )
            intelligence_summary += (
                f" Risk/strategy research: status={risk_strategy.status.value}; "
                f"volatility={risk_strategy.volatility.regime.value}; scenarios: {scenario_states}. "
                "Scenario quality is an evidence-completeness score, not a probability, recommendation, "
                "approval, or execution instruction."
            )
        preparation = (
            f"Research preparation for {context.symbol}"
            + (f" on {research.timeframe}" if research and research.timeframe else "")
            + "."
        )

        technical = context.technical_analysis
        if technical is not None and technical.analyses:
            calculated = "; ".join(
                f"{item.timeframe} trend={item.trend_direction.value}, "
                f"indicators={sum(feature.status is FeatureStatus.AVAILABLE for feature in item.indicators)}/"
                f"{len(item.indicators)}, structure={','.join(item.structure_observations) or 'unconfirmed'}"
                for item in technical.analyses
            )
            missing = sum(
                feature.status is not FeatureStatus.AVAILABLE
                for item in technical.analyses
                for feature in item.indicators
            )
            directional = {
                item.trend_direction
                for item in technical.analyses
                if item.trend_direction
                in {TrendDirection.BULLISH, TrendDirection.BEARISH, TrendDirection.MIXED}
            }
            conflict = " Conflicting timeframe classifications are present." if len(directional) > 1 else ""
            final_response = (
                f"{preparation} Calculated observations: {calculated}. "
                "Interpretation: these deterministic rules describe trend and mathematical price "
                f"structure; they are not a trading signal. Missing evidence: {missing} configured "
                f"indicator value(s) remain unavailable.{conflict} "
                "Current quote and positioning evidence remain separate and are not inferred."
                f"{intelligence_summary}"
            )
        else:
            final_response = (
                f"{preparation} Deterministic research frameworks: {result_digest} "
                "Validated technical calculations are unavailable because no suitable historical "
                "series was assigned. Other missing evidence remains unavailable."
                f"{intelligence_summary}"
            )

        agreement_agent_ids = list(coordination.agreement_agent_ids) if coordination else []
        disagreement_agent_ids = (
            list(coordination.disagreement_agent_ids) if coordination else []
        )
        missing_agent_ids = list(coordination.missing_agent_ids) if coordination else []
        evidence_references = list(
            {
                (reference.source_id, reference.event_id): reference
                for result in completed_results
                for reference in result.evidence_references
            }.values()
        )
        conditional_scenarios: list[str] = []
        if risk_strategy is not None:
            conditional_scenarios.extend(
                f"{item.direction.value.title()} scenario: {item.status.value}; "
                f"entry={item.entry_zone.reference_price if item.entry_zone is not None else 'unavailable'}, "
                f"invalidation={item.stop_loss_candidate if item.stop_loss_candidate is not None else 'unavailable'}, "
                f"risk/reward={item.risk_reward.ratio if item.risk_reward.ratio is not None else 'unavailable'}. "
                "Research only; not a trade instruction."
                for item in risk_strategy.scenarios
            )
        if specialist_synthesis is not None:
            if not evidence_references:
                evidence_references = [
                    AgentEvidenceLink(
                        event_id=item.event_id,
                        source_id=item.source_id,
                        domain=item.domain.value,
                        observed_at=item.timestamp,
                        symbol=item.symbol,
                    )
                    for item in specialist_synthesis.supporting_evidence
                ]
            if specialist_synthesis.consensus is SpecialistConsensus.AGREEMENT:
                stances = {
                    item.stance
                    for item in specialist_synthesis.specialist_analyses
                    if item.stance in {SpecialistStance.BULLISH, SpecialistStance.BEARISH}
                }
                if stances == {SpecialistStance.BULLISH}:
                    conditional_scenarios.append(
                        "Bullish research scenario is conditional on the cited evidence remaining "
                        "valid; it is not a trade instruction."
                    )
                elif stances == {SpecialistStance.BEARISH}:
                    conditional_scenarios.append(
                        "Bearish research scenario is conditional on the cited evidence remaining "
                        "valid; it is not a trade instruction."
                    )
        if disagreement_agent_ids:
            final_response += (
                " Specialist disagreement remains unresolved between: "
                + ", ".join(disagreement_agent_ids)
                + ". No artificial consensus was applied."
            )
        if missing_agent_ids:
            final_response += (
                " Missing or unavailable specialists: "
                + ", ".join(missing_agent_ids)
                + "."
            )

        return Synthesis(
            status="completed",
            agents_consulted=consulted,
            summary=f"F.R.I.D.A.Y. — The Master routed {context.symbol} to {agent_names}.",
            final_response=final_response,
            caveat=(
                "Deterministic calculations only; no AI model, broker, MT5 connection, order "
                "placement, production authorization, or execution capability was used."
            ),
            agreement_agent_ids=agreement_agent_ids,
            disagreement_agent_ids=disagreement_agent_ids,
            missing_agent_ids=missing_agent_ids,
            evidence_references=evidence_references,
            conditional_scenarios=conditional_scenarios,
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
        master_thesis: MasterTradeThesis | None = None,
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
        if master_thesis is not None and (
            master_synthesis is None
            or master_thesis.analysis_id != analysis_id
            or master_thesis.evidence_fingerprint != evidence_fingerprint
            or master_thesis.master_synthesis_id != master_synthesis.synthesis_id
        ):
            raise ValueError("Master thesis must reference the supplied analysis and Master synthesis")

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
            master_thesis_reference=(master_thesis.as_reference() if master_thesis else None),
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

    def create_trade_thesis(
        self,
        *,
        analysis_id: str,
        symbol: str | None,
        evidence_fingerprint: str,
        evidence_as_of: datetime,
        master_synthesis: MasterSpecialistSynthesis,
    ) -> MasterTradeThesis:
        """Build one immutable analytical thesis with no approval authority."""
        return self._decision_service.build(
            analysis_id=analysis_id,
            symbol=symbol,
            evidence_fingerprint=evidence_fingerprint,
            evidence_as_of=evidence_as_of,
            master_synthesis=master_synthesis,
        )
