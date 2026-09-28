from hashlib import sha256

from app.agents.base import AnalysisContext, BaseAgent
from app.agents.master import MasterAgent
from app.agents.specialists import create_specialist_agents
from app.models.schemas import AgentAnalysis, AnalyzeRequest, AnalyzeResponse, RoutingDecision


class NimbusOrchestrator:
    def __init__(self, specialists: list[BaseAgent], master: MasterAgent) -> None:
        self._specialists = specialists
        self._master = master

    def route(self, query: str) -> RoutingDecision:
        matched_ids = {
            agent.agent_id
            for agent in self._specialists
            if agent.supports(query)
        }

        if matched_ids:
            matched_ids.update({"risk", "journal"})
            reason = "Selected agents by deterministic keyword routing; Risk and Journal are always included."
        else:
            matched_ids = {agent.agent_id for agent in self._specialists}
            reason = "No specialist keywords matched, so the orchestrator selected the full desk."

        selected_agents = [
            agent.agent_id
            for agent in self._specialists
            if agent.agent_id in matched_ids
        ]
        return RoutingDecision(selected_agents=selected_agents, reason=reason)

    def analyze(self, request: AnalyzeRequest) -> AnalyzeResponse:
        context = AnalysisContext(symbol=request.symbol, query=request.query)
        routing = self.route(request.query)
        selected = set(routing.selected_agents)
        results: list[AgentAnalysis] = []

        for agent in self._specialists:
            if agent.agent_id not in selected:
                results.append(agent.skipped())
                continue

            try:
                results.append(agent.analyze(context))
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

        completed_results = [result for result in results if result.status == "completed"]
        synthesis = self._master.synthesize(context, completed_results)
        digest = sha256(f"{request.symbol}\n{request.query}".encode()).hexdigest()[:16]

        return AnalyzeResponse(
            analysis_id=f"mock-{digest}",
            mode="deterministic_mock",
            symbol=request.symbol,
            query=request.query,
            routing=routing,
            agent_results=results,
            synthesis=synthesis,
        )


def create_orchestrator() -> NimbusOrchestrator:
    return NimbusOrchestrator(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
    )
