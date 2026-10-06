from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.evidence.models import (
    AgentEvidence,
    CorporateActionEvidence,
    EconomicEventEvidence,
    EvidenceProvenance,
    EstimateObservationEvidence,
    EstimateSeriesEvidence,
    FundamentalObservationEvidence,
    FundamentalSeriesEvidence,
    IssuerReferenceEvidence,
    MarketEvidence,
    MarketSeriesEvidence,
    MacroObservationEvidence,
    MacroSeriesEvidence,
    NewsEvidence,
    PositioningObservationEvidence,
    PositioningSeriesEvidence,
    PortfolioRiskEvidence,
    canonical_symbol,
)
from app.models.derived_features import DerivedMarketFeature
from app.models.specialist_analysis import (
    SpecialistAnalysis,
    SpecialistAnalysisStatus,
    SpecialistDerivedFeatureReference,
    SpecialistDerivedObservation,
    SpecialistCorporateActionObservation,
    SpecialistEconomicObservation,
    SpecialistEvidenceReference,
    SpecialistFactObservation,
    SpecialistFundamentalObservation,
    SpecialistIssuerReferenceObservation,
    SpecialistEstimateObservation,
    SpecialistInterpretation,
    SpecialistLifecycleObservation,
    SpecialistMacroObservation,
    SpecialistOperationalState,
    SpecialistPositioningObservation,
    SpecialistPortfolioRiskObservation,
    SpecialistPortfolioContextState,
    SpecialistStance,
    create_specialist_analysis,
)
from app.models.schemas import AgentAnalysis


@dataclass(frozen=True, slots=True)
class PriorAgentResult:
    """A transient, provider-neutral record available to Ledger during one run."""

    agent_id: str
    agent_name: str
    status: Literal["completed", "skipped", "error"]
    summary: str
    confidence: float


@dataclass(frozen=True, slots=True)
class AnalysisLifecycle:
    """Immutable metadata describing what triggered one analysis run."""

    trigger_type: Literal["user_query", "market_event", "news_event"] = "user_query"
    trigger_event_id: str | None = None
    prior_results: tuple[PriorAgentResult, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    symbol: str
    query: str
    evidence: AgentEvidence = field(default_factory=AgentEvidence)
    derived_features: tuple[DerivedMarketFeature, ...] = ()
    lifecycle: AnalysisLifecycle = field(default_factory=AnalysisLifecycle)


class BaseAgent(ABC):
    """Contract shared by deterministic agents and future model-backed agents."""

    agent_id: str
    name: str
    expertise: str
    routing_keywords: frozenset[str] = frozenset()

    def supports(self, query: str) -> bool:
        normalized_query = query.casefold()
        return any(keyword in normalized_query for keyword in self.routing_keywords)

    @abstractmethod
    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        """Return one structured analysis result without side effects."""

    def skipped(self) -> AgentAnalysis:
        return AgentAnalysis(
            agent_id=self.agent_id,
            agent_name=self.name,
            status="skipped",
            summary="Not selected by the orchestrator for this request.",
            findings=[],
            confidence=0.0,
            limitations=["No analysis was run."],
        )

    @staticmethod
    def evidence_findings(context: AnalysisContext) -> list[str]:
        """Describe only the provider-neutral evidence assigned to this specialist."""
        market_count = len(context.evidence.market_evidence)
        news_count = len(context.evidence.news_evidence)
        series_count = len(context.evidence.market_series)
        economic_count = len(context.evidence.economic_events)
        macro_count = len(context.evidence.macro_series)
        positioning_count = sum(
            len(series.observations) for series in context.evidence.positioning_series
        )
        fundamental_count = sum(
            len(series.observations) for series in context.evidence.fundamental_series
        )
        estimate_count = sum(
            len(series.observations) for series in context.evidence.estimate_series
        )
        corporate_action_count = len(context.evidence.corporate_actions)
        issuer_reference_count = len(context.evidence.issuer_references)
        portfolio_risk_count = len(context.evidence.portfolio_risk_snapshots)
        if (
            market_count == 0
            and news_count == 0
            and series_count == 0
            and economic_count == 0
            and macro_count == 0
            and positioning_count == 0
            and fundamental_count == 0
            and estimate_count == 0
            and corporate_action_count == 0
            and issuer_reference_count == 0
            and portfolio_risk_count == 0
        ):
            return []

        assignment = (
            "Structured evidence assigned for this analysis: "
            f"{market_count} market item(s) and {news_count} news item(s)."
        )
        if series_count:
            assignment += f" Historical series assigned: {series_count}."
        if economic_count:
            assignment += f" Economic events assigned: {economic_count}."
        if macro_count:
            assignment += f" Macro series assigned: {macro_count}."
        if positioning_count:
            assignment += f" Positioning observations assigned: {positioning_count}."
        if fundamental_count:
            assignment += f" Fundamental observations assigned: {fundamental_count}."
        if estimate_count:
            assignment += f" Estimate observations assigned: {estimate_count}."
        if corporate_action_count:
            assignment += f" Corporate actions assigned: {corporate_action_count}."
        if issuer_reference_count:
            assignment += f" Issuer reference profiles assigned: {issuer_reference_count}."
        if portfolio_risk_count:
            assignment += f" Portfolio-risk snapshots assigned: {portfolio_risk_count}."
        findings = [assignment]
        if context.evidence.provenance:
            findings.append(
                f"Evidence provenance retained for {len(context.evidence.provenance)} item(s)."
            )
        if context.lifecycle.prior_results:
            findings.append(
                "Prior specialist-result metadata is available for this analysis lifecycle: "
                f"{len(context.lifecycle.prior_results)} result(s)."
            )
        return findings

    def result(
        self,
        *,
        summary: str,
        findings: list[str],
        limitations: list[str],
        confidence: float = 0.6,
    ) -> AgentAnalysis:
        return AgentAnalysis(
            agent_id=self.agent_id,
            agent_name=self.name,
            status="completed",
            summary=summary,
            findings=findings,
            confidence=confidence,
            limitations=limitations,
        )

    def structured_analysis(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
    ) -> SpecialistAnalysis:
        """Return an internal artifact without changing the public result contract.

        The default is deliberately conservative: deterministic mock specialists
        have no basis for a directional stance unless a future adapter overrides
        this method with directly supported observations.
        """
        return self._structured_result(
            context,
            parent_analysis_id=parent_analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            created_at=created_at,
            result=result,
            stance=SpecialistStance.INSUFFICIENT_DATA,
            thesis=(
                f"{self.name} has no supported directional conclusion from the assigned "
                "provider-neutral evidence."
            ),
            interpretations=(
                SpecialistInterpretation(
                    statement=(
                        "The deterministic specialist output is a research framework, not a "
                        "directional market conclusion."
                    )
                ),
            ),
        )

    def _structured_result(
        self,
        context: AnalysisContext,
        *,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
        result: AgentAnalysis,
        stance: SpecialistStance,
        thesis: str,
        fact_observations: tuple[SpecialistFactObservation, ...] = (),
        interpretations: tuple[SpecialistInterpretation, ...] = (),
        derived_observations: tuple[SpecialistDerivedObservation, ...] = (),
        economic_observations: tuple[SpecialistEconomicObservation, ...] = (),
        macro_observations: tuple[SpecialistMacroObservation, ...] = (),
        positioning_observations: tuple[SpecialistPositioningObservation, ...] = (),
        fundamental_observations: tuple[SpecialistFundamentalObservation, ...] = (),
        estimate_observations: tuple[SpecialistEstimateObservation, ...] = (),
        corporate_action_observations: tuple[SpecialistCorporateActionObservation, ...] = (),
        issuer_reference_observations: tuple[SpecialistIssuerReferenceObservation, ...] = (),
        portfolio_risk_observations: tuple[SpecialistPortfolioRiskObservation, ...] = (),
        portfolio_context_state: SpecialistPortfolioContextState | None = None,
        invalidating_conditions: tuple[str, ...] = (),
        operational_state: SpecialistOperationalState | None = None,
        lifecycle: SpecialistLifecycleObservation | None = None,
    ) -> SpecialistAnalysis:
        """Create one safe artifact tied to this specialist's assigned evidence."""
        supporting_evidence = self._supporting_evidence(context)
        derived_feature_references = tuple(
            self.derived_feature_reference(feature)
            for feature in context.derived_features
        )
        timeframes = tuple(
            evidence.timeframe
            for evidence in context.evidence.market_evidence
            if evidence.timeframe
        ) + tuple(series.timeframe for series in context.evidence.market_series)
        if self.agent_id == "ledger_the_book" and lifecycle is None:
            lifecycle = SpecialistLifecycleObservation(
                trigger_type=context.lifecycle.trigger_type,
                trigger_event_id=context.lifecycle.trigger_event_id,
                prior_agent_ids=tuple(
                    result.agent_id for result in context.lifecycle.prior_results
                ),
            )
        if self.agent_id == "pilot_execution" and operational_state is None:
            operational_state = SpecialistOperationalState.DISABLED_NO_EXECUTION
        if self.agent_id == "sentinel_risk_officer" and portfolio_context_state is None:
            portfolio_context_state = (
                SpecialistPortfolioContextState.PORTFOLIO_CONTEXT_AVAILABLE
                if portfolio_risk_observations
                else SpecialistPortfolioContextState.PORTFOLIO_CONTEXT_INSUFFICIENT
            )

        return create_specialist_analysis(
            parent_analysis_id=parent_analysis_id,
            agent_id=self.agent_id,
            status=SpecialistAnalysisStatus(result.status),
            created_at=created_at,
            evidence_fingerprint=evidence_fingerprint,
            # No current deterministic specialist has enough validated evidence
            # to support a directional confidence value.
            confidence=result.confidence if stance is not SpecialistStance.INSUFFICIENT_DATA else 0.0,
            stance=stance,
            thesis=thesis,
            fact_observations=fact_observations,
            interpretations=interpretations,
            derived_observations=derived_observations,
            economic_observations=economic_observations,
            macro_observations=macro_observations,
            positioning_observations=positioning_observations,
            fundamental_observations=fundamental_observations,
            estimate_observations=estimate_observations,
            corporate_action_observations=corporate_action_observations,
            issuer_reference_observations=issuer_reference_observations,
            portfolio_risk_observations=portfolio_risk_observations,
            portfolio_context_state=portfolio_context_state,
            supporting_evidence=supporting_evidence,
            derived_feature_references=derived_feature_references,
            invalidating_conditions=invalidating_conditions,
            limitations=tuple(result.limitations),
            timeframes=timeframes,
            lifecycle=lifecycle,
            operational_state=operational_state,
        )

    @staticmethod
    def market_reference(evidence: MarketEvidence) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.provenance.event_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.provenance.event_timestamp,
            symbol=canonical_symbol(evidence.symbol),
        )

    @staticmethod
    def news_reference(
        evidence: NewsEvidence,
        *,
        symbol: str | None,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.provenance.event_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.provenance.event_timestamp,
            symbol=canonical_symbol(symbol),
        )

    @staticmethod
    def series_reference(
        evidence: MarketSeriesEvidence,
    ) -> SpecialistEvidenceReference:
        latest_timestamp = (
            evidence.latest_completed_at
            if evidence.latest_completed_at is not None
            else evidence.provenance.latest_received_at
        )
        return SpecialistEvidenceReference(
            event_id=evidence.series_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=latest_timestamp,
            symbol=canonical_symbol(evidence.symbol),
        )

    @staticmethod
    def economic_reference(
        evidence: EconomicEventEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.event_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.released_at or evidence.scheduled_at,
            symbol=None,
        )

    @staticmethod
    def macro_reference(
        evidence: MacroObservationEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.observation_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=(
                evidence.published_at
                or evidence.observed_at
                or evidence.effective_at
                or evidence.received_at
            ),
            symbol=None,
        )

    @staticmethod
    def macro_series_references(
        evidence: MacroSeriesEvidence,
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return tuple(BaseAgent.macro_reference(item) for item in evidence.observations)

    @staticmethod
    def positioning_reference(
        evidence: PositioningObservationEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.observation_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=(
                evidence.published_at
                or evidence.observed_at
                or evidence.vintage_at
                or evidence.received_at
            ),
            symbol=None,
        )

    @staticmethod
    def positioning_series_references(
        evidence: PositioningSeriesEvidence,
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return tuple(BaseAgent.positioning_reference(item) for item in evidence.observations)

    @staticmethod
    def fundamental_reference(
        evidence: FundamentalObservationEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.observation_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=(
                evidence.published_at
                or evidence.filed_at
                or evidence.reported_at
                or evidence.vintage_at
                or evidence.received_at
            ),
            symbol=None,
        )

    @staticmethod
    def fundamental_series_references(
        evidence: FundamentalSeriesEvidence,
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return tuple(BaseAgent.fundamental_reference(item) for item in evidence.observations)

    @staticmethod
    def estimate_reference(
        evidence: EstimateObservationEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.observation_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.published_at or evidence.vintage_at or evidence.as_of,
            symbol=None,
        )

    @staticmethod
    def estimate_series_references(
        evidence: EstimateSeriesEvidence,
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return tuple(BaseAgent.estimate_reference(item) for item in evidence.observations)

    @staticmethod
    def corporate_action_reference(
        evidence: CorporateActionEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.action_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.effective_at or evidence.published_at,
            symbol=evidence.instrument.symbol,
        )

    @staticmethod
    def issuer_reference_reference(
        evidence: IssuerReferenceEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.profile_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.published_at or evidence.as_of,
            symbol=evidence.instrument.source_symbol,
        )

    @staticmethod
    def portfolio_risk_reference(
        evidence: PortfolioRiskEvidence,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.snapshot_id,
            source_id=evidence.provenance.source_id,
            domain=evidence.provenance.domain,
            timestamp=evidence.published_at or evidence.as_of,
            symbol=None,
        )

    @staticmethod
    def provenance_reference(
        evidence: EvidenceProvenance,
    ) -> SpecialistEvidenceReference:
        return SpecialistEvidenceReference(
            event_id=evidence.event_id,
            source_id=evidence.source_id,
            domain=evidence.domain,
            timestamp=evidence.event_timestamp,
            symbol=None,
        )

    @staticmethod
    def derived_feature_reference(
        feature: DerivedMarketFeature,
    ) -> SpecialistDerivedFeatureReference:
        """Return derived lineage without presenting it as provider evidence."""
        return SpecialistDerivedFeatureReference.from_feature(feature)

    def _supporting_evidence(
        self,
        context: AnalysisContext,
    ) -> tuple[SpecialistEvidenceReference, ...]:
        return tuple(
            self.market_reference(evidence) for evidence in context.evidence.market_evidence
        ) + tuple(
            self.news_reference(evidence, symbol=context.symbol)
            for evidence in context.evidence.news_evidence
        ) + tuple(
            self.series_reference(evidence) for evidence in context.evidence.market_series
        ) + tuple(
            self.economic_reference(evidence)
            for evidence in context.evidence.economic_events
        ) + tuple(
            reference
            for evidence in context.evidence.macro_series
            for reference in self.macro_series_references(evidence)
        ) + tuple(
            reference
            for evidence in context.evidence.positioning_series
            for reference in self.positioning_series_references(evidence)
        ) + tuple(
            reference
            for evidence in context.evidence.fundamental_series
            for reference in self.fundamental_series_references(evidence)
        ) + tuple(
            reference
            for evidence in context.evidence.estimate_series
            for reference in self.estimate_series_references(evidence)
        ) + tuple(
            self.corporate_action_reference(evidence)
            for evidence in context.evidence.corporate_actions
        ) + tuple(
            self.issuer_reference_reference(evidence)
            for evidence in context.evidence.issuer_references
        ) + tuple(
            self.portfolio_risk_reference(evidence)
            for evidence in context.evidence.portfolio_risk_snapshots
        ) + tuple(
            self.provenance_reference(evidence)
            for evidence in context.evidence.provenance
        )
