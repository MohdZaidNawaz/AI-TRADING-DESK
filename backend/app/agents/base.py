from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.evidence.models import (
    AgentEvidence,
    MarketEvidence,
    MarketSeriesEvidence,
    NewsEvidence,
    canonical_symbol,
)
from app.models.derived_features import DerivedMarketFeature
from app.models.specialist_analysis import (
    SpecialistAnalysis,
    SpecialistAnalysisStatus,
    SpecialistDerivedFeatureReference,
    SpecialistDerivedObservation,
    SpecialistEvidenceReference,
    SpecialistFactObservation,
    SpecialistInterpretation,
    SpecialistLifecycleObservation,
    SpecialistOperationalState,
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
        if market_count == 0 and news_count == 0 and series_count == 0:
            return []

        assignment = (
            "Structured evidence assigned for this analysis: "
            f"{market_count} market item(s) and {news_count} news item(s)."
        )
        if series_count:
            assignment += f" Historical series assigned: {series_count}."
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
        )
