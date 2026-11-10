from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from threading import Event, Lock
from time import perf_counter

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
from app.models.derived_features import (
    DerivedFeatureRequest,
    DerivedFeatureSnapshot,
    feature_snapshot_identity,
)
from app.models.proposal_assembly import ExplicitTradeProposalInput
from app.models.proposals import RiskContext, TradeProposalWorkflowResult
from app.models.specialist_analysis import MasterSpecialistSynthesis, SpecialistAnalysis
from app.models.agent_coordination import (
    AgentCoordinationReport,
    AgentEvidenceLink,
    AgentExecutionRecord,
    AgentExecutionState,
    AgentExecutionTransition,
    AgentOutputKind,
    AgentTaskPlan,
    CoordinationConsensus,
    SpecialistRole,
)
from app.models.research_requests import (
    HistoricalRetrievalDiagnostic,
    InstrumentResolutionStatus,
    ResearchRequestContext,
)
from app.models.trade_thesis import MasterTradeThesis
from app.models.technical_analysis import TechnicalAnalysisReport
from app.models.news_intelligence import (
    NewsEconomicIntelligenceReport,
    NewsRetrievalDiagnostic,
    NewsRetrievalStatus,
)
from app.models.macro_intelligence import MacroIntelligenceReport
from app.models.liquidity_intelligence import LiquidityIntelligenceReport
from app.models.risk_strategy_intelligence import RiskStrategyIntelligenceReport
from app.services.trade_proposal_workflow import TradeProposalWorkflow, build_evidence_bundle
from app.services.proposal_assembly import ProposalAssemblyService
from app.services.derived_feature_routing import DerivedFeatureRouter
from app.services.derived_features import (
    DEFAULT_DERIVED_FEATURE_REQUESTS,
    DerivedFeatureService,
)
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService
from app.services.research_requests import ResearchRequestService
from app.services.technical_analysis import TechnicalAnalysisService
from app.services.news_intelligence import NewsEconomicIntelligenceService
from app.services.macro_intelligence import MacroIntelligenceService
from app.services.liquidity_intelligence import LiquidityIntelligenceService
from app.services.risk_strategy_intelligence import RiskStrategyIntelligenceService
from app.services.macro_policies import CANONICAL_MACRO_INDICATORS


AnalyzeResponse.model_rebuild(
    _types_namespace={
        "TechnicalAnalysisReport": TechnicalAnalysisReport,
        "NewsEconomicIntelligenceReport": NewsEconomicIntelligenceReport,
        "MacroIntelligenceReport": MacroIntelligenceReport,
        "LiquidityIntelligenceReport": LiquidityIntelligenceReport,
        "RiskStrategyIntelligenceReport": RiskStrategyIntelligenceReport,
    }
)


@dataclass(frozen=True, slots=True)
class _AgentRunArtifacts:
    """Private pairing of API-facing results and internal specialist artifacts."""

    results: tuple[AgentAnalysis, ...]
    specialist_analyses: tuple[SpecialistAnalysis, ...]
    coordination: AgentCoordinationReport


@dataclass(frozen=True, slots=True)
class _AnalysisRun:
    """Private Phase 4 result; public API responses remain unchanged."""

    analysis: AnalyzeResponse
    specialist_analyses: tuple[SpecialistAnalysis, ...]
    master_synthesis: MasterSpecialistSynthesis
    master_thesis: MasterTradeThesis


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
        proposal_assembly_service: ProposalAssemblyService | None = None,
        trusted_execution_evidence: TrustedExecutionEvidenceService | None = None,
        research_request_service: ResearchRequestService | None = None,
        technical_analysis_service: TechnicalAnalysisService | None = None,
        news_intelligence_service: NewsEconomicIntelligenceService | None = None,
        macro_intelligence_service: MacroIntelligenceService | None = None,
        liquidity_intelligence_service: LiquidityIntelligenceService | None = None,
        risk_strategy_intelligence_service: RiskStrategyIntelligenceService | None = None,
        agent_timeout_seconds: float = 10.0,
    ) -> None:
        specialist_ids = [agent.agent_id for agent in specialists]
        if len(specialist_ids) != len(set(specialist_ids)):
            raise ValueError("Specialist agent IDs must be unique")
        if agent_timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
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
        self._proposal_assembly_service = proposal_assembly_service or ProposalAssemblyService()
        self._trusted_execution_evidence = trusted_execution_evidence or TrustedExecutionEvidenceService()
        self._research_request_service = research_request_service or ResearchRequestService()
        self._technical_analysis_service = technical_analysis_service or TechnicalAnalysisService(
            feature_service=self._derived_feature_service
        )
        self._news_intelligence_service = (
            news_intelligence_service or NewsEconomicIntelligenceService()
        )
        self._macro_intelligence_service = macro_intelligence_service or MacroIntelligenceService()
        self._liquidity_intelligence_service = (
            liquidity_intelligence_service or LiquidityIntelligenceService()
        )
        self._risk_strategy_intelligence_service = (
            risk_strategy_intelligence_service or RiskStrategyIntelligenceService()
        )
        self._recent_market_events: deque[NormalizedDataEvent] = deque(maxlen=100)
        self._event_lock = Lock()
        self._agent_timeout_seconds = agent_timeout_seconds
        self._coordination_lock = Lock()
        self._active_cancellations: dict[str, Event] = {}

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
        technical_analysis = self._technical_analysis_service.analyze(
            evidence_snapshot.market_series
        )
        evidence_bundle = build_evidence_bundle(evidence_snapshot)
        feature_snapshot = self._feature_snapshot_with_technical(
            evidence_snapshot,
            technical_analysis,
        )
        context = replace(
            context,
            technical_analysis=technical_analysis,
            liquidity_intelligence=(
                self._liquidity_intelligence_service.analyze(
                    evidence_snapshot.market_series,
                    evaluated_at=evidence_bundle.created_at,
                )
                if evidence_snapshot.market_series
                else None
            ),
        )
        context = replace(
            context,
            risk_strategy_intelligence=self._risk_strategy_intelligence_service.analyze(
                subject=context.symbol,
                technical=context.technical_analysis,
                liquidity=context.liquidity_intelligence,
                macro=context.macro_intelligence,
                news=context.news_intelligence,
                market_series=evidence_snapshot.market_series,
                evaluated_at=evidence_bundle.created_at,
            ),
        )
        return list(
            self._run_agents_with_artifacts(
                context,
                selected_agents,
                snapshot=evidence_snapshot,
                feature_snapshot=feature_snapshot,
                parent_analysis_id="standalone-agent-run",
                evidence_fingerprint="0" * 64,
                created_at=evidence_bundle.created_at,
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
        """Coordinate specialists in dependency waves while preserving roster order."""
        planned_at = datetime.now(UTC)
        agents_by_id = {agent.agent_id: agent for agent in self._specialists}
        order = tuple(agents_by_id)
        dependencies = self._coordination_dependencies(selected_agents, order)
        tasks = tuple(
            AgentTaskPlan(
                task_id=f"{parent_analysis_id}:{agent_id}",
                agent_id=agent_id,
                role=self._specialist_role(agent_id),
                selected=agent_id in selected_agents,
                dependency_agent_ids=dependencies[agent_id],
            )
            for agent_id in order
        )
        cancellation = Event()
        with self._coordination_lock:
            self._active_cancellations[parent_analysis_id] = cancellation

        results_by_id: dict[str, AgentAnalysis] = {}
        artifacts_by_id: dict[str, SpecialistAnalysis] = {}
        records_by_id: dict[str, AgentExecutionRecord] = {}
        contexts_by_id: dict[str, AnalysisContext] = {}

        def context_for(agent_id: str) -> AnalysisContext:
            dependency_results = (
                tuple(
                    PriorAgentResult(
                        agent_id=results_by_id[item].agent_id,
                        agent_name=results_by_id[item].agent_name,
                        status=results_by_id[item].status,
                        summary=results_by_id[item].summary,
                        confidence=results_by_id[item].confidence,
                    )
                    for item in dependencies[agent_id]
                    if item in results_by_id
                )
                if agent_id in {"athena_analyst", "sentinel_risk_officer"}
                else ()
            )
            if agent_id == "ledger_the_book":
                dependency_results = tuple(
                    PriorAgentResult(
                        agent_id=results_by_id[item].agent_id,
                        agent_name=results_by_id[item].agent_name,
                        status=results_by_id[item].status,
                        summary=results_by_id[item].summary,
                        confidence=results_by_id[item].confidence,
                    )
                    for item in order
                    if item in results_by_id and item != agent_id
                )
            return replace(
                context,
                evidence=self._evidence_router.route(agent_id=agent_id, snapshot=snapshot),
                derived_features=self._derived_feature_router.route(
                    agent_id=agent_id,
                    snapshot=feature_snapshot,
                ),
                lifecycle=replace(context.lifecycle, prior_results=dependency_results),
            )

        def terminal_result(
            agent: BaseAgent,
            *,
            status: str,
            summary: str,
            limitation: str,
        ) -> AgentAnalysis:
            return AgentAnalysis(
                agent_id=agent.agent_id,
                agent_name=agent.name,
                status=status,
                summary=summary,
                findings=[],
                confidence=0.0,
                limitations=[limitation],
            )

        try:
            pending = set(order)
            while pending:
                ready = [
                    agent_id
                    for agent_id in order
                    if agent_id in pending
                    and all(item in results_by_id for item in dependencies[agent_id])
                ]
                if not ready:  # pragma: no cover - fixed acyclic policy plus constructor guard
                    raise RuntimeError("Agent dependency plan contains a cycle")

                runnable: list[str] = []
                for agent_id in ready:
                    pending.remove(agent_id)
                    agent = agents_by_id[agent_id]
                    agent_context = context_for(agent_id)
                    contexts_by_id[agent_id] = agent_context
                    now = datetime.now(UTC)
                    if agent_id not in selected_agents:
                        result = agent.skipped().model_copy(
                            update={
                                "execution_state": AgentExecutionState.UNAVAILABLE,
                                "completed_at": now,
                                "missing_inputs": ["agent_not_selected"],
                                "output_kinds": [AgentOutputKind.UNAVAILABLE],
                            }
                        )
                        results_by_id[agent_id] = result
                        records_by_id[agent_id] = self._execution_record(
                            task=next(item for item in tasks if item.agent_id == agent_id),
                            state=AgentExecutionState.UNAVAILABLE,
                            planned_at=planned_at,
                            started_at=None,
                            completed_at=now,
                            missing_inputs=("agent_not_selected",),
                            output_kinds=(AgentOutputKind.UNAVAILABLE,),
                        )
                        continue
                    runnable.append(agent_id)

                if not runnable:
                    continue
                if cancellation.is_set():
                    for agent_id in runnable:
                        agent = agents_by_id[agent_id]
                        now = datetime.now(UTC)
                        results_by_id[agent_id] = terminal_result(
                            agent,
                            status="error",
                            summary="Agent analysis was cancelled.",
                            limitation="Analysis cancellation requested.",
                        ).model_copy(
                            update={
                                "execution_state": AgentExecutionState.FAILED,
                                "completed_at": now,
                                "output_kinds": [AgentOutputKind.UNAVAILABLE],
                            }
                        )
                        records_by_id[agent_id] = self._execution_record(
                            task=next(item for item in tasks if item.agent_id == agent_id),
                            state=AgentExecutionState.FAILED,
                            planned_at=planned_at,
                            started_at=None,
                            completed_at=now,
                            error_category="cancelled",
                            output_kinds=(AgentOutputKind.UNAVAILABLE,),
                        )
                    continue

                started = datetime.now(UTC)
                start_clock = perf_counter()
                executor = ThreadPoolExecutor(
                    max_workers=len(runnable),
                    thread_name_prefix="friday-specialist",
                )
                futures: dict[Future[AgentAnalysis], str] = {
                    executor.submit(agents_by_id[agent_id].analyze, contexts_by_id[agent_id]): agent_id
                    for agent_id in runnable
                }
                done, not_done = wait(futures, timeout=self._agent_timeout_seconds)
                completed = datetime.now(UTC)
                for future in done:
                    agent_id = futures[future]
                    agent = agents_by_id[agent_id]
                    if cancellation.is_set():
                        result = terminal_result(
                            agent,
                            status="error",
                            summary="Agent analysis was cancelled.",
                            limitation="Analysis cancellation requested; completed output was discarded.",
                        )
                        state = AgentExecutionState.FAILED
                        error_category = "cancelled"
                    else:
                        try:
                            result = future.result()
                            state = self._classify_execution_state(
                                agent_id,
                                result,
                                contexts_by_id[agent_id],
                            )
                            error_category = None
                        except Exception as exc:  # defensive adapter boundary
                            result = terminal_result(
                                agent,
                                status="error",
                                summary="Agent analysis failed.",
                                limitation=f"Internal agent error: {type(exc).__name__}",
                            )
                            state = AgentExecutionState.FAILED
                            error_category = "agent_error"
                    kinds = self._output_kinds(state, contexts_by_id[agent_id])
                    missing = self._missing_inputs(agent_id, state, contexts_by_id[agent_id])
                    results_by_id[agent_id] = result.model_copy(
                        update={
                            "execution_state": state,
                            "started_at": started,
                            "completed_at": completed,
                            "missing_inputs": list(missing),
                            "output_kinds": list(kinds),
                        }
                    )
                    records_by_id[agent_id] = self._execution_record(
                        task=next(item for item in tasks if item.agent_id == agent_id),
                        state=state,
                        planned_at=planned_at,
                        started_at=started,
                        completed_at=completed,
                        duration_ms=max(0, round((perf_counter() - start_clock) * 1000)),
                        error_category=error_category,
                        missing_inputs=missing,
                        output_kinds=kinds,
                    )
                for future in not_done:
                    agent_id = futures[future]
                    future.cancel()
                    agent = agents_by_id[agent_id]
                    result = terminal_result(
                        agent,
                        status="error",
                        summary="Agent analysis exceeded its bounded execution time.",
                        limitation="Agent execution timed out; late output was discarded.",
                    ).model_copy(
                        update={
                            "execution_state": AgentExecutionState.FAILED,
                            "started_at": started,
                            "completed_at": completed,
                            "output_kinds": [AgentOutputKind.UNAVAILABLE],
                        }
                    )
                    results_by_id[agent_id] = result
                    records_by_id[agent_id] = self._execution_record(
                        task=next(item for item in tasks if item.agent_id == agent_id),
                        state=AgentExecutionState.FAILED,
                        planned_at=planned_at,
                        started_at=started,
                        completed_at=completed,
                        duration_ms=max(0, round((perf_counter() - start_clock) * 1000)),
                        error_category="cancelled" if cancellation.is_set() else "timeout",
                        output_kinds=(AgentOutputKind.UNAVAILABLE,),
                    )
                executor.shutdown(wait=False, cancel_futures=True)

            for agent_id in order:
                agent = agents_by_id[agent_id]
                result = results_by_id[agent_id]
                agent_context = contexts_by_id[agent_id]
                try:
                    artifact = agent.structured_analysis(
                        agent_context,
                        parent_analysis_id=parent_analysis_id,
                        evidence_fingerprint=evidence_fingerprint,
                        created_at=created_at,
                        result=result,
                    )
                except Exception:  # keep malformed adapters from escaping coordination
                    artifact = BaseAgent.structured_analysis(
                        agent,
                        agent_context,
                        parent_analysis_id=parent_analysis_id,
                        evidence_fingerprint=evidence_fingerprint,
                        created_at=created_at,
                        result=result,
                    )
                artifacts_by_id[agent_id] = artifact
                evidence_links = [
                    AgentEvidenceLink(
                        event_id=item.event_id,
                        source_id=item.source_id,
                        domain=item.domain.value,
                        observed_at=item.timestamp,
                        retrieved_at=self._retrieval_timestamp(
                            agent_context,
                            event_id=item.event_id,
                            source_id=item.source_id,
                        ),
                        symbol=item.symbol,
                    )
                    for item in artifact.supporting_evidence
                ]
                methods = sorted(
                    {item.method_id for item in artifact.derived_feature_references}
                )
                results_by_id[agent_id] = result.model_copy(
                    update={
                        "evidence_references": evidence_links,
                        "calculation_methods": methods,
                    }
                )
                records_by_id[agent_id] = records_by_id[agent_id].model_copy(
                    update={"evidence_reference_count": len(evidence_links)}
                )
        finally:
            with self._coordination_lock:
                self._active_cancellations.pop(parent_analysis_id, None)

        completed_at = datetime.now(UTC)
        missing = tuple(
            agent_id
            for agent_id in order
            if records_by_id[agent_id].state
            in {AgentExecutionState.FAILED, AgentExecutionState.UNAVAILABLE}
        )
        coordination = AgentCoordinationReport(
            correlation_id=parent_analysis_id,
            planned_at=planned_at,
            completed_at=completed_at,
            tasks=tasks,
            executions=tuple(records_by_id[item] for item in order),
            consensus=CoordinationConsensus.INSUFFICIENT_DATA,
            missing_agent_ids=missing,
        )
        return _AgentRunArtifacts(
            results=tuple(results_by_id[item] for item in order),
            specialist_analyses=tuple(artifacts_by_id[item] for item in order),
            coordination=coordination,
        )

    def cancel_analysis(self, correlation_id: str) -> bool:
        """Request bounded cancellation; running adapter output is discarded safely."""
        with self._coordination_lock:
            cancellation = self._active_cancellations.get(correlation_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True

    @staticmethod
    def _specialist_role(agent_id: str) -> SpecialistRole:
        return {
            "atlas_macro": SpecialistRole.MACRO,
            "capitol_smart_money": SpecialistRole.LIQUIDITY,
            "scout_recon": SpecialistRole.NEWS_SESSION,
            "athena_analyst": SpecialistRole.STRATEGY,
            "chartist_technician": SpecialistRole.TECHNICAL,
            "oracle_quant": SpecialistRole.QUANTITATIVE,
            "sentinel_risk_officer": SpecialistRole.RISK,
            "pilot_execution": SpecialistRole.EXECUTION_CONTROL,
            "ledger_the_book": SpecialistRole.JOURNAL,
        }[agent_id]

    @staticmethod
    def _coordination_dependencies(
        selected_agents: set[str],
        order: tuple[str, ...],
    ) -> dict[str, tuple[str, ...]]:
        present = set(order)
        configured = {
            "athena_analyst": (
                "atlas_macro",
                "scout_recon",
                "chartist_technician",
                "oracle_quant",
            ),
            "sentinel_risk_officer": ("chartist_technician", "oracle_quant"),
            "pilot_execution": ("sentinel_risk_officer",),
            "ledger_the_book": tuple(item for item in order if item != "ledger_the_book"),
        }
        return {
            agent_id: tuple(
                dependency
                for dependency in configured.get(agent_id, ())
                if dependency in present and dependency in selected_agents
            )
            for agent_id in order
        }

    @staticmethod
    def _has_routed_evidence(context: AnalysisContext) -> bool:
        evidence = context.evidence
        return any(
            (
                evidence.market_evidence,
                evidence.news_evidence,
                evidence.market_series,
                evidence.economic_events,
                evidence.macro_series,
                evidence.positioning_series,
                evidence.fundamental_series,
                evidence.estimate_series,
                evidence.corporate_actions,
                evidence.issuer_references,
                evidence.portfolio_risk_snapshots,
                evidence.portfolio_risk_metric_snapshots,
                evidence.provenance,
            )
        )

    @classmethod
    def _classify_execution_state(
        cls,
        agent_id: str,
        result: AgentAnalysis,
        context: AnalysisContext,
    ) -> AgentExecutionState:
        if result.status == "error":
            return AgentExecutionState.FAILED
        if result.status == "skipped" or agent_id == "pilot_execution":
            return AgentExecutionState.UNAVAILABLE
        has_input = cls._has_routed_evidence(context) or bool(context.derived_features)
        if agent_id in {"chartist_technician", "oracle_quant"}:
            has_input = has_input or bool(
                context.technical_analysis and context.technical_analysis.analyses
            )
        elif agent_id == "atlas_macro":
            has_input = has_input or context.macro_intelligence is not None
        elif agent_id == "scout_recon":
            has_input = has_input or context.news_intelligence is not None
        elif agent_id == "capitol_smart_money":
            has_input = has_input or context.liquidity_intelligence is not None
        elif agent_id in {"athena_analyst", "sentinel_risk_officer", "ledger_the_book"}:
            has_input = has_input or bool(context.lifecycle.prior_results)
        if not has_input:
            return AgentExecutionState.UNAVAILABLE
        return (
            AgentExecutionState.PARTIAL
            if result.limitations
            else AgentExecutionState.COMPLETED
        )

    @classmethod
    def _output_kinds(
        cls,
        state: AgentExecutionState,
        context: AnalysisContext,
    ) -> tuple[AgentOutputKind, ...]:
        if state in {AgentExecutionState.FAILED, AgentExecutionState.UNAVAILABLE}:
            return (AgentOutputKind.UNAVAILABLE,)
        kinds: list[AgentOutputKind] = []
        if cls._has_routed_evidence(context):
            kinds.append(AgentOutputKind.VERIFIED_OBSERVATION)
        if (
            context.derived_features
            or context.technical_analysis
            or context.macro_intelligence
            or context.liquidity_intelligence
        ):
            kinds.append(AgentOutputKind.DETERMINISTIC_CALCULATION)
        kinds.append(AgentOutputKind.INTERPRETATION)
        return tuple(dict.fromkeys(kinds))

    @staticmethod
    def _missing_inputs(
        agent_id: str,
        state: AgentExecutionState,
        context: AnalysisContext,
    ) -> tuple[str, ...]:
        if state is AgentExecutionState.COMPLETED:
            return ()
        missing: list[str] = []
        if agent_id in {"chartist_technician", "oracle_quant"} and not (
            context.technical_analysis and context.technical_analysis.analyses
        ):
            missing.append("validated_historical_ohlcv")
        if agent_id == "atlas_macro" and context.macro_intelligence is None:
            missing.append("validated_macro_evidence")
        if agent_id == "scout_recon" and context.news_intelligence is None:
            missing.append("validated_news_or_session_evidence")
        if agent_id == "capitol_smart_money":
            if context.liquidity_intelligence is None:
                missing.append("validated_historical_ohlcv_for_liquidity_calculations")
        if agent_id == "pilot_execution":
            missing.append("execution_capability_disabled")
        if agent_id == "ledger_the_book" and not context.lifecycle.prior_results:
            missing.append("historical_journal_records")
        return tuple(dict.fromkeys(missing))

    @staticmethod
    def _retrieval_timestamp(
        context: AnalysisContext,
        *,
        event_id: str,
        source_id: str,
    ) -> datetime | None:
        evidence = context.evidence
        direct = (
            *evidence.market_evidence,
            *evidence.news_evidence,
            *evidence.economic_events,
        )
        for item in direct:
            provenance = item.provenance
            if provenance.event_id == event_id and provenance.source_id == source_id:
                return provenance.received_at
        series = (
            *evidence.market_series,
            *evidence.macro_series,
            *evidence.positioning_series,
            *evidence.fundamental_series,
            *evidence.estimate_series,
        )
        for item in series:
            if item.series_id == event_id and item.provenance.source_id == source_id:
                return item.provenance.latest_received_at
            for observation in getattr(item, "observations", ()):
                identity = getattr(
                    observation,
                    "observation_id",
                    getattr(observation.provenance, "event_id", None),
                )
                if identity == event_id and observation.provenance.source_id == source_id:
                    return observation.received_at
        identified = (
            (evidence.corporate_actions, "action_id"),
            (evidence.issuer_references, "profile_id"),
            (evidence.portfolio_risk_snapshots, "snapshot_id"),
            (evidence.portfolio_risk_metric_snapshots, "metrics_snapshot_id"),
        )
        for items, identity_field in identified:
            for item in items:
                if (
                    getattr(item, identity_field) == event_id
                    and item.provenance.source_id == source_id
                ):
                    return item.provenance.received_at
        for provenance in evidence.provenance:
            if provenance.event_id == event_id and provenance.source_id == source_id:
                return provenance.received_at
        return None

    @staticmethod
    def _execution_record(
        *,
        task: AgentTaskPlan,
        state: AgentExecutionState,
        planned_at: datetime,
        started_at: datetime | None,
        completed_at: datetime,
        duration_ms: int = 0,
        error_category: str | None = None,
        missing_inputs: tuple[str, ...] = (),
        output_kinds: tuple[AgentOutputKind, ...] = (),
    ) -> AgentExecutionRecord:
        transitions = [
            AgentExecutionTransition(state=AgentExecutionState.PENDING, at=planned_at)
        ]
        if started_at is not None:
            transitions.append(
                AgentExecutionTransition(state=AgentExecutionState.RUNNING, at=started_at)
            )
        transitions.append(AgentExecutionTransition(state=state, at=completed_at))
        return AgentExecutionRecord(
            task_id=task.task_id,
            agent_id=task.agent_id,
            state=state,
            transitions=tuple(transitions),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            dependency_agent_ids=task.dependency_agent_ids,
            error_category=error_category,
            evidence_reference_count=0,
            missing_inputs=missing_inputs,
            output_kinds=output_kinds,
        )

    def prepare_research_request(self, request: AnalyzeRequest) -> ResearchRequestContext:
        """Resolve a request without fetching data or granting any authority."""
        return self._research_request_service.prepare(request)

    def analyze(
        self,
        request: AnalyzeRequest,
        *,
        historical_retrieval: HistoricalRetrievalDiagnostic | None = None,
        news_retrieval: NewsRetrievalDiagnostic | None = None,
    ) -> AnalyzeResponse:
        """Analyze a query against the current symbol-scoped evidence snapshot."""
        research_context = self._research_request_service.prepare(request)
        resolved_symbol = (
            research_context.primary_instrument.canonical_symbol
            if request.symbol is None
            and research_context.primary_instrument.status
            is InstrumentResolutionStatus.RESOLVED
            else request.symbol
        )
        snapshot = self._evidence_store.snapshot(symbol=resolved_symbol)
        if news_retrieval is not None and (
            news_retrieval.evidence_ids or news_retrieval.cached_evidence_ids
        ):
            selected_ids = frozenset(
                (*news_retrieval.evidence_ids, *news_retrieval.cached_evidence_ids)
            )
            selected_news = tuple(
                item
                for item in self._evidence_store.snapshot().news_evidence
                if item.provenance.event_id in selected_ids
            )
            snapshot = replace(snapshot, news_evidence=selected_news)
        return self._analyze_run(
            request,
            snapshot=snapshot,
            lifecycle=AnalysisLifecycle(),
            research_context=research_context,
            historical_retrieval=historical_retrieval,
            news_retrieval=news_retrieval,
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
            master_thesis=analysis_run.master_thesis,
        )

    def run_explicit_trade_proposal_workflow(
        self,
        request: AnalyzeRequest,
        *,
        proposal_input: ExplicitTradeProposalInput,
        risk_context: RiskContext | None = None,
    ) -> TradeProposalWorkflowResult:
        """Explicitly assemble and submit a final proposal for Sentinel review.

        This internal-only method is never invoked by ``analyze``.  It captures
        one immutable snapshot, creates no trade values itself, and forwards only
        the caller-supplied final proposal to Sentinel.
        """
        snapshot = self._evidence_store.snapshot(symbol=request.symbol)
        analysis_run = self._analyze_run(
            request,
            snapshot=snapshot,
            lifecycle=AnalysisLifecycle(),
        )
        proposal = self._proposal_assembly_service.assemble(
            master_thesis=analysis_run.master_thesis,
            snapshot=snapshot,
            analysis_id=analysis_run.analysis.analysis_id,
            proposal_input=proposal_input,
        )
        return self._trade_proposal_workflow.run_explicit(
            analysis=analysis_run.analysis,
            snapshot=snapshot,
            risk_context=risk_context,
            proposal=proposal,
        )

    def _analyze_run(
        self,
        request: AnalyzeRequest,
        *,
        snapshot: EvidenceSnapshot,
        lifecycle: AnalysisLifecycle,
        research_context: ResearchRequestContext | None = None,
        historical_retrieval: HistoricalRetrievalDiagnostic | None = None,
        news_retrieval: NewsRetrievalDiagnostic | None = None,
    ) -> _AnalysisRun:
        prepared_context = research_context or self._research_request_service.prepare(request)
        requested_timeframes = (
            (prepared_context.timeframe,) if prepared_context.timeframe is not None else ()
        )
        technical_analysis = self._technical_analysis_service.analyze(
            snapshot.market_series,
            requested_timeframes=requested_timeframes,
        )
        feature_snapshot = self._feature_snapshot_with_technical(
            snapshot,
            technical_analysis,
        )
        effective_subject = (
            request.symbol
            or prepared_context.primary_instrument.canonical_symbol
            or self._DEFAULT_SUBJECT
        )
        canonical_macro_ids = {item.indicator_id for item in CANONICAL_MACRO_INDICATORS}
        has_canonical_macro = any(
            series.key.indicator_id in canonical_macro_ids for series in snapshot.macro_series
        ) or any(
            event.indicator_id in canonical_macro_ids
            for event in snapshot.economic_events
            if event.indicator_id is not None
        )
        macro_intelligence = (
            self._macro_intelligence_service.analyze(
                subject=effective_subject,
                snapshot=snapshot,
            )
            if has_canonical_macro
            else None
        )
        news_intelligence = None
        if (
            snapshot.news_evidence
            or snapshot.economic_events
            or (
                news_retrieval is not None
                and news_retrieval.status
                not in {
                    NewsRetrievalStatus.NOT_REQUESTED,
                    NewsRetrievalStatus.UNSUPPORTED,
                }
            )
        ):
            news_intelligence = self._news_intelligence_service.analyze(
                subject=effective_subject,
                snapshot=snapshot,
                retrieval=news_retrieval,
            )
        prepared_context = self._research_request_service.assess_evidence(
            prepared_context,
            snapshot,
            derived_feature_count=len(feature_snapshot.features),
            historical_retrieval=historical_retrieval,
        )
        effective_symbol = request.symbol or (
            prepared_context.primary_instrument.canonical_symbol
            if prepared_context.primary_instrument.status
            is InstrumentResolutionStatus.RESOLVED
            else None
        )
        evidence_bundle = build_evidence_bundle(snapshot)
        liquidity_intelligence = (
            self._liquidity_intelligence_service.analyze(
                snapshot.market_series,
                requested_timeframes=requested_timeframes,
                evaluated_at=evidence_bundle.created_at,
            )
            if snapshot.market_series
            else None
        )
        has_risk_strategy_evidence = bool(
            technical_analysis.analyses
            or liquidity_intelligence is not None
        )
        risk_strategy_intelligence = (
            self._risk_strategy_intelligence_service.analyze(
                subject=effective_symbol or self._DEFAULT_SUBJECT,
                technical=(technical_analysis if technical_analysis.analyses else None),
                liquidity=liquidity_intelligence,
                macro=macro_intelligence,
                news=news_intelligence,
                market_series=snapshot.market_series,
                evaluated_at=evidence_bundle.created_at,
            )
            if has_risk_strategy_evidence
            else None
        )
        context = AnalysisContext(
            symbol=effective_symbol or self._DEFAULT_SUBJECT,
            query=request.query,
            lifecycle=lifecycle,
            research_context=prepared_context,
            technical_analysis=(technical_analysis if technical_analysis.analyses else None),
            news_intelligence=news_intelligence,
            macro_intelligence=macro_intelligence,
            liquidity_intelligence=liquidity_intelligence,
            risk_strategy_intelligence=risk_strategy_intelligence,
        )
        selected_routing = self.select_agents(request.query)
        routing = selected_routing.model_copy(update={"research_context": prepared_context})
        digest = sha256(f"{request.symbol or ''}\n{request.query}".encode()).hexdigest()[:16]
        analysis_id = f"mock-{digest}"
        agent_run = self._run_agents_with_artifacts(
            context,
            set(routing.selected_agents),
            snapshot=snapshot,
            feature_snapshot=feature_snapshot,
            parent_analysis_id=analysis_id,
            evidence_fingerprint=evidence_bundle.fingerprint,
            created_at=evidence_bundle.created_at,
        )

        master_synthesis = self._master.synthesize_specialist_analyses(
            analysis_id=analysis_id,
            evidence_fingerprint=evidence_bundle.fingerprint,
            specialist_analyses=agent_run.specialist_analyses,
        )
        agent_by_analysis = {
            item.analysis_id: item.agent_id for item in agent_run.specialist_analyses
        }
        agreement_agent_ids = (
            tuple(
                agent_by_analysis[item]
                for item in master_synthesis.participating_analysis_ids
                if item in agent_by_analysis
            )
            if master_synthesis.consensus.value == "agreement"
            else ()
        )
        disagreement_agent_ids = tuple(
            agent_by_analysis[item]
            for item in master_synthesis.conflicting_analysis_ids
            if item in agent_by_analysis
        )
        coordination = agent_run.coordination.model_copy(
            update={
                "consensus": CoordinationConsensus(master_synthesis.consensus.value),
                "agreement_agent_ids": agreement_agent_ids,
                "disagreement_agent_ids": disagreement_agent_ids,
            }
        )
        completed_results = [
            result
            for result in agent_run.results
            if result.status == "completed"
        ]
        synthesis = self._master.synthesize(
            context,
            completed_results,
            specialist_synthesis=master_synthesis,
            coordination=coordination,
        )
        master_thesis = self._master.create_trade_thesis(
            analysis_id=analysis_id,
            # Research resolution is deliberately not an execution binding.
            # Proposal workflows continue to receive only the caller's explicit
            # legacy symbol field.
            symbol=request.symbol,
            evidence_fingerprint=evidence_bundle.fingerprint,
            evidence_as_of=evidence_bundle.created_at,
            master_synthesis=master_synthesis,
        )
        analysis = AnalyzeResponse(
            analysis_id=analysis_id,
            mode="deterministic_mock",
            symbol=effective_symbol,
            query=request.query,
            selected_agents=routing.selected_agents,
            routing=routing,
            agent_results=list(agent_run.results),
            synthesis=synthesis,
            coordination=coordination,
            technical_analysis=(technical_analysis if technical_analysis.analyses else None),
            news_intelligence=news_intelligence,
            macro_intelligence=macro_intelligence,
            liquidity_intelligence=context.liquidity_intelligence,
            risk_strategy_intelligence=context.risk_strategy_intelligence,
        )
        return _AnalysisRun(
            analysis=analysis,
            specialist_analyses=agent_run.specialist_analyses,
            master_synthesis=master_synthesis,
            master_thesis=master_thesis,
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

    def _feature_snapshot_with_technical(
        self,
        snapshot: EvidenceSnapshot,
        technical_analysis: TechnicalAnalysisReport,
    ) -> DerivedFeatureSnapshot:
        base = self._calculate_derived_features(snapshot)
        technical = tuple(
            feature
            for analysis in technical_analysis.analyses
            for feature in analysis.indicators
        )
        unique = {feature.feature_id: feature for feature in (*base.features, *technical)}
        features = tuple(unique[key] for key in sorted(unique))
        return DerivedFeatureSnapshot(
            snapshot_id=feature_snapshot_identity(features),
            features=features,
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

    @property
    def trusted_execution_evidence(self) -> TrustedExecutionEvidenceService:
        """Application-composed, read-only evidence boundary; unavailable by default."""
        return self._trusted_execution_evidence


def create_friday_core(
    *,
    sentinel_gate=None,
    trusted_execution_evidence=None,
    research_request_service=None,
    technical_analysis_service=None,
) -> FridayCore:
    """Build FRIDAY with an optionally application-composed Sentinel gate."""
    master = MasterAgent()
    return FridayCore(
        specialists=create_specialist_agents(),
        master=master,
        trade_proposal_workflow=(
            TradeProposalWorkflow(master=master, sentinel_gate=sentinel_gate)
            if sentinel_gate is not None
            else None
        ),
        trusted_execution_evidence=trusted_execution_evidence,
        research_request_service=research_request_service,
        technical_analysis_service=technical_analysis_service,
    )


# Keep the previous public names available to avoid breaking existing imports.
NimbusOrchestrator = FridayCore


def create_orchestrator() -> FridayCore:
    return create_friday_core()
