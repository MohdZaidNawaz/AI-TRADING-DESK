from collections import deque
from dataclasses import replace
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
from app.evidence import EvidenceRouter, EvidenceSnapshot, EvidenceStore
from app.models.schemas import (
    AgentAnalysis,
    AnalyzeRequest,
    AnalyzeResponse,
    RoutingDecision,
)
from app.models.events import NormalizedDataEvent, NormalizedNewsResearchEvent


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
    ) -> None:
        self._specialists = specialists
        self._master = master
        self._evidence_store = evidence_store or EvidenceStore()
        self._evidence_router = evidence_router or EvidenceRouter()
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
        results: list[AgentAnalysis] = []
        evidence_snapshot = snapshot or EvidenceSnapshot()

        for agent in self._specialists:
            if agent.agent_id not in selected_agents:
                results.append(agent.skipped())
                continue

            try:
                agent_context = replace(
                    context,
                    evidence=self._evidence_router.route(
                        agent_id=agent.agent_id,
                        snapshot=evidence_snapshot,
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
                results.append(agent.analyze(agent_context))
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                results.append(
                    AgentAnalysis(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        status="error",
                        summary="Agent analysis failed.",
                        findings=[],
                        confidence=0.0,
                        limitations=[f"Internal agent error: {type(exc).__name__}"],
                    )
                )

        return results

    def analyze(self, request: AnalyzeRequest) -> AnalyzeResponse:
        """Analyze a query against the current symbol-scoped evidence snapshot."""
        return self._analyze(
            request,
            snapshot=self._evidence_store.snapshot(symbol=request.symbol),
            lifecycle=AnalysisLifecycle(),
        )

    def _analyze(
        self,
        request: AnalyzeRequest,
        *,
        snapshot: EvidenceSnapshot,
        lifecycle: AnalysisLifecycle,
    ) -> AnalyzeResponse:
        context = AnalysisContext(
            symbol=request.symbol or self._DEFAULT_SUBJECT,
            query=request.query,
            lifecycle=lifecycle,
        )
        routing = self.select_agents(request.query)
        results = self.run_agents(
            context,
            set(routing.selected_agents),
            snapshot=snapshot,
        )

        completed_results = [result for result in results if result.status == "completed"]
        synthesis = self._master.synthesize(context, completed_results)
        digest = sha256(f"{request.symbol or ''}\n{request.query}".encode()).hexdigest()[:16]

        return AnalyzeResponse(
            analysis_id=f"mock-{digest}",
            mode="deterministic_mock",
            symbol=request.symbol,
            query=request.query,
            selected_agents=routing.selected_agents,
            routing=routing,
            agent_results=results,
            synthesis=synthesis,
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
        return self._analyze(
            AnalyzeRequest(symbol=event.symbol, query=query),
            snapshot=self._evidence_store.snapshot(symbol=event.symbol),
            lifecycle=AnalysisLifecycle(
                trigger_type="market_event",
                trigger_event_id=event.event_id,
            ),
        )

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
