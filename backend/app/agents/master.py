from app.agents.base import AnalysisContext
from app.models.schemas import AgentAnalysis, Synthesis


class MasterAgent:
    """Deterministic synthesis layer; replaceable by a model-backed adapter later."""

    agent_id = "master"
    name = "FRIDAY Master"

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
            summary=f"FRIDAY Core routed {context.symbol} to {agent_names}.",
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
