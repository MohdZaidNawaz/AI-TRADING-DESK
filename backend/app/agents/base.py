from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models.schemas import AgentAnalysis


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    symbol: str
    query: str


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
