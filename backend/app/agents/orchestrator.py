from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from threading import Lock

from app.agents.base import (
    AnalysisContext,
    AnalysisLifecycle,
    BaseAgent,
    PriorAgentResult,
)
from app.agents.master import MasterAgent
from app.agents.specialists import create_specialist_agents
from app.evidence import (
    EvidenceRouter,
    EvidenceSnapshot,
    EvidenceStore,
    MarketSeriesEvidence,
)
from app.models.schemas import (
    AgentAnalysis,
    AnalyzeRequest,
    AnalyzeResponse,
    RoutingDecision,
)
from app.models.events import NormalizedDataEvent, NormalizedNewsResearchEvent
from app.models.market_series import NormalizedMarketBar
from app.models.economic_events import NormalizedEconomicEvent
from app.models.macro_series import NormalizedMacroObservation
from app.models.positioning import NormalizedPositioningObservation
from app.models.fundamentals import NormalizedFundamentalObservation
from app.models.estimates import NormalizedEstimateObservation
from app.models.corporate_actions import NormalizedCorporateAction
from app.models.issuer_reference import NormalizedIssuerInstrumentReference
from app.models.portfolio_risk import NormalizedPortfolioRiskSnapshot
from app.models.derived_features import DerivedFeatureRequest, DerivedFeatureSnapshot
from app.models.proposals import RiskContext, TradeProposalWorkflowResult
from app.models.specialist_analysis import MasterSpecialistSynthesis, SpecialistAnalysis
from app.services.trade_proposal_workflow import TradeProposalWorkflow, build_evidence_bundle
from app.services.derived_feature_routing import DerivedFeatureRouter
from app.services.derived_features import (
    DEFAULT_DERIVED_FEATURE_REQUESTS,
    DerivedFeatureService,
)


@dataclass(frozen=True, slots=True)
class _AgentRunArtifacts:
    """Private pairing of API-facing results and internal specialist artifacts."""

    results: tuple[AgentAnalysis, ...]
    specialist_analyses: tuple[SpecialistAnalysis, ...]


@dataclass(frozen=True, slots=True)
class _AnalysisRun:
    """Private Phase 4 result; public API responses remain unchanged."""

    analysis: AnalyzeResponse
    specialist_analyses: tuple[SpecialistAnalysis, ...]
    master_synthesis: MasterSpecialistSynthesis


class FridayCore:
    """Coordinates deterministic specialist analysis and master synthesis.

    The core depends only on the shared agent contract, so deterministic agents can
    later be replaced with data- or model-backed adapters without changing the API.
    """

    _DEFAULT_SUBJECT = "MARKET CONTEXT"

    def __init__(
        self,
        specialists: list[BaseAgent],
        master: MasterAgent,
        *,
        evidence_store: EvidenceStore | None = None,
        evidence_router: EvidenceRouter | None = None,
        derived_feature_service: DerivedFeatureService | None = None,
        derived_feature_router: DerivedFeatureRouter | None = None,
        derived_feature_requests: tuple[DerivedFeatureRequest, ...] | None = None,
        trade_proposal_workflow: TradeProposalWorkflow | None = None,
    ) -> None:
        self._specialists = specialists
        self._master = master
        self._evidence_store = evidence_store or EvidenceStore()
        self._evidence_router = evidence_router or EvidenceRouter()
        self._derived_feature_service = derived_feature_service or DerivedFeatureService()
        self._derived_feature_router = derived_feature_router or DerivedFeatureRouter()
        self._derived_feature_requests = (
            DEFAULT_DERIVED_FEATURE_REQUESTS
            if derived_feature_requests is None
            else derived_feature_requests
        )
        self._trade_proposal_workflow = trade_proposal_workflow or TradeProposalWorkflow(
            master=self._master
        )
        self._recent_market_events: deque[NormalizedDataEvent] = deque(maxlen=100)
        self._event_lock = Lock()

    def select_agents(self, query: str) -> RoutingDecision:
        matched_ids = {
            agent.agent_id
            for agent in self._specialists
            if agent.supports(query)
        }

        if matched_ids:
            matched_ids.update({"sentinel_risk_officer", "ledger_the_book"})
            reason = (
                "Selected agents by deterministic keyword routing; Sentinel Risk Officer and "
                "Ledger The Book are always included."
            )
        else:
            matched_ids = {agent.agent_id for agent in self._specialists}
            reason = "No specialist keywords matched, so the orchestrator selected the full desk."

        selected_agents = [
            agent.agent_id
            for agent in self._specialists
            if agent.agent_id in matched_ids
        ]
        return RoutingDecision(selected_agents=selected_agents, reason=reason)

    def route(self, query: str) -> RoutingDecision:
        """Compatibility alias for the previous orchestration interface."""
        return self.select_agents(query)

    def run_agents(
        self,
        context: AnalysisContext,
        selected_agents: set[str],
        *,
        snapshot: EvidenceSnapshot | None = None,
    ) -> list[AgentAnalysis]:
        """Run selected specialists and preserve a status for the rest of the desk."""
        evidence_snapshot = snapshot or EvidenceSnapshot()
        feature_snapshot = self._calculate_derived_features(evidence_snapshot)
        return list(
            self._run_agents_with_artifacts(
                context,
                selected_agents,
                snapshot=evidence_snapshot,
                feature_snapshot=feature_snapshot,
                parent_analysis_id="standalone-agent-run",
                evidence_fingerprint="0" * 64,
                created_at=build_evidence_bundle(evidence_snapshot).created_at,
            ).results
        )

    def _run_agents_with_artifacts(
        self,
        context: AnalysisContext,
        selected_agents: set[str],
        *,
        snapshot: EvidenceSnapshot,
        feature_snapshot: DerivedFeatureSnapshot,
        parent_analysis_id: str,
        evidence_fingerprint: str,
        created_at: datetime,
    ) -> _AgentRunArtifacts:
        """Run all specialists against one supplied snapshot and retain internal artifacts."""
        results: list[AgentAnalysis] = []
        specialist_analyses: list[SpecialistAnalysis] = []

        for agent in self._specialists:
            agent_context = replace(
                context,
                evidence=self._evidence_router.route(
                    agent_id=agent.agent_id,
                    snapshot=snapshot,
                ),
                derived_features=self._derived_feature_router.route(
                    agent_id=agent.agent_id,
                    snapshot=feature_snapshot,
                ),
                lifecycle=replace(
                    context.lifecycle,
                    prior_results=(
                        tuple(
                            PriorAgentResult(
                                agent_id=result.agent_id,
                                agent_name=result.agent_name,
                                status=result.status,
                                summary=result.summary,
                                confidence=result.confidence,
                            )
                            for result in results
                        )
                        if agent.agent_id == "ledger_the_book"
                        else ()
                    ),
                ),
            )
            if agent.agent_id not in selected_agents:
                result = agent.skipped()
                results.append(result)
                specialist_analyses.append(
                    agent.structured_analysis(
                        agent_context,
                        parent_analysis_id=parent_analysis_id,
                        evidence_fingerprint=evidence_fingerprint,
                        created_at=created_at,
                        result=result,
                    )
                )
                continue

            try:
                result = agent.analyze(agent_context)
                results.append(result)
                specialist_analyses.append(
                    agent.structured_analysis(
                        agent_context,
                        parent_analysis_id=parent_analysis_id,
                        evidence_fingerprint=evidence_fingerprint,
                        created_at=created_at,
                        result=result,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                result = AgentAnalysis(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    status="error",
                    summary="Agent analysis failed.",
                    findings=[],
                    confidence=0.0,
                    limitations=[f"Internal agent error: {type(exc).__name__}"],
                )
                results.append(result)
                specialist_analyses.append(
                    BaseAgent.structured_analysis(
                        agent,
                        agent_context,
                        parent_analysis_id=parent_analysis_id,
                        evidence_fingerprint=evidence_fingerprint,
                        created_at=created_at,
                        result=result,
                    )
                )

        return _AgentRunArtifacts(
            results=tuple(results),
            specialist_analyses=tuple(specialist_analyses),
        )

    def analyze(self, request: AnalyzeRequest) -> AnalyzeResponse:
        """Analyze a query against the current symbol-scoped evidence snapshot."""
        return self._analyze_run(
            request,
            snapshot=self._evidence_store.snapshot(symbol=request.symbol),
            lifecycle=AnalysisLifecycle(),
        ).analysis

    def run_trade_proposal_workflow(
        self,
        request: AnalyzeRequest,
        *,
        risk_context: RiskContext | None = None,
    ) -> TradeProposalWorkflowResult:
        """Run the internal Phase 3 proposal gate without changing `/api/analyze`.

        One symbol-scoped snapshot is captured exactly once and is shared by
        specialist analysis, Master draft creation, and Sentinel evaluation.
        """
        snapshot = self._evidence_store.snapshot(symbol=request.symbol)
        analysis_run = self._analyze_run(
            request,
            snapshot=snapshot,
            lifecycle=AnalysisLifecycle(),
        )
        return self._trade_proposal_workflow.run(
            analysis=analysis_run.analysis,
            snapshot=snapshot,
            risk_context=risk_context,
            specialist_analyses=analysis_run.specialist_analyses,
            master_synthesis=analysis_run.master_synthesis,
        )

    def _analyze_run(
        self,
        request: AnalyzeRequest,
        *,
        snapshot: EvidenceSnapshot,
        lifecycle: AnalysisLifecycle,
    ) -> _AnalysisRun:
        context = AnalysisContext(
            symbol=request.symbol or self._DEFAULT_SUBJECT,
            query=request.query,
            lifecycle=lifecycle,
        )
        routing = self.select_agents(request.query)
        digest = sha256(f"{request.symbol or ''}\n{request.query}".encode()).hexdigest()[:16]
        analysis_id = f"mock-{digest}"
        evidence_bundle = build_evidence_bundle(snapshot)
        feature_snapshot = self._calculate_derived_features(snapshot)
        agent_run = self._run_agents_with_artifacts(
            context,
            set(routing.selected_agents),
            snapshot=snapshot,
            feature_snapshot=feature_snapshot,
            parent_analysis_id=analysis_id,
            evidence_fingerprint=evidence_bundle.fingerprint,
            created_at=evidence_bundle.created_at,
        )

        completed_results = [
            result for result in agent_run.results if result.status == "completed"
        ]
        synthesis = self._master.synthesize(context, completed_results)
        master_synthesis = self._master.synthesize_specialist_analyses(
            analysis_id=analysis_id,
            evidence_fingerprint=evidence_bundle.fingerprint,
            specialist_analyses=agent_run.specialist_analyses,
        )
        analysis = AnalyzeResponse(
            analysis_id=analysis_id,
            mode="deterministic_mock",
            symbol=request.symbol,
            query=request.query,
            selected_agents=routing.selected_agents,
            routing=routing,
            agent_results=list(agent_run.results),
            synthesis=synthesis,
        )
        return _AnalysisRun(
            analysis=analysis,
            specialist_analyses=agent_run.specialist_analyses,
            master_synthesis=master_synthesis,
        )

    def _calculate_derived_features(
        self,
        snapshot: EvidenceSnapshot,
    ) -> DerivedFeatureSnapshot:
        """Calculate one immutable feature snapshot for the entire analysis run."""
        return self._derived_feature_service.calculate_snapshot(
            snapshot.market_series,
            self._derived_feature_requests,
        )

    def ingest_event(self, event: NormalizedDataEvent) -> AnalyzeResponse:
        """Consume a provider-neutral event through the existing FRIDAY analysis flow.

        Connectors and transports must normalize provider payloads before invoking
        this boundary, so agent routing stays independent of any external source.
        """
        with self._event_lock:
            self._recent_market_events.append(event)
        self._evidence_store.ingest_market_event(event)

        subject = event.symbol or self._DEFAULT_SUBJECT
        query = (
            f"{event.source.display_name} {event.event} event for {subject}. "
            "Review technical structure, market surveillance, and risk context."
        )
        return self._analyze_run(
            AnalyzeRequest(symbol=event.symbol, query=query),
            snapshot=self._evidence_store.snapshot(symbol=event.symbol),
            lifecycle=AnalysisLifecycle(
                trigger_type="market_event",
                trigger_event_id=event.event_id,
            ),
        ).analysis

    def ingest_market_event(self, event: NormalizedDataEvent) -> AnalyzeResponse:
        """Compatibility entry point for the existing market-event integration."""
        return self.ingest_event(event)

    def ingest_news_event(
        self,
        event: NormalizedNewsResearchEvent,
    ) -> NormalizedNewsResearchEvent:
        """Stage provider-neutral news evidence without triggering retrieval or execution."""
        self._evidence_store.ingest_news_event(event)
        return event

    def ingest_market_bar(
        self,
        bar: NormalizedMarketBar,
    ) -> MarketSeriesEvidence | None:
        """Stage a validated bar without triggering analysis or execution."""
        return self._evidence_store.ingest_market_bar(bar)

    def ingest_economic_event(self, event: NormalizedEconomicEvent) -> object:
        """Stage a normalized economic lifecycle version without analysis or execution."""
        return self._evidence_store.ingest_economic_event(event)

    def ingest_macro_observation(self, observation: NormalizedMacroObservation) -> object:
        """Stage a normalized macro observation without analysis or execution."""
        return self._evidence_store.ingest_macro_observation(observation)

    def ingest_positioning_observation(
        self,
        observation: NormalizedPositioningObservation,
    ) -> object:
        """Stage reported positioning evidence without analysis or execution."""
        return self._evidence_store.ingest_positioning_observation(observation)

    def ingest_fundamental_observation(
        self,
        observation: NormalizedFundamentalObservation,
    ) -> object:
        """Stage a financial-statement observation without analysis or execution."""
        return self._evidence_store.ingest_fundamental_observation(observation)

    def ingest_estimate_observation(
        self,
        observation: NormalizedEstimateObservation,
    ) -> object:
        """Stage a source-reported estimate without analysis or execution."""
        return self._evidence_store.ingest_estimate_observation(observation)

    def ingest_corporate_action(self, action: NormalizedCorporateAction) -> object:
        """Stage one source-reported action without analysis or execution."""
        return self._evidence_store.ingest_corporate_action(action)

    def ingest_issuer_reference(
        self,
        profile: NormalizedIssuerInstrumentReference,
    ) -> object:
        """Stage source-reported issuer/listing context without analysis or execution."""
        return self._evidence_store.ingest_issuer_reference(profile)

    def ingest_portfolio_risk_snapshot(
        self,
        snapshot: NormalizedPortfolioRiskSnapshot,
    ) -> object:
        """Stage source-reported portfolio context without analysis or execution."""
        return self._evidence_store.ingest_portfolio_risk_snapshot(snapshot)

    @property
    def recent_market_events(self) -> tuple[NormalizedDataEvent, ...]:
        """Expose a snapshot for future event-aware agent adapters."""
        with self._event_lock:
            return tuple(self._recent_market_events)

    @property
    def evidence_store(self) -> EvidenceStore:
        """Return the process-local store used to create immutable snapshots."""
        return self._evidence_store

    def evidence_snapshot(self, *, symbol: str | None = None) -> EvidenceSnapshot:
        """Expose an immutable, provider-neutral snapshot for internal consumers."""
        return self._evidence_store.snapshot(symbol=symbol)


def create_friday_core() -> FridayCore:
    return FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
    )


# Keep the previous public names available to avoid breaking existing imports.
NimbusOrchestrator = FridayCore


def create_orchestrator() -> FridayCore:
    return create_friday_core()
