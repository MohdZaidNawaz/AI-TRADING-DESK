from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from app.evidence.models import AgentEvidence
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
        if market_count == 0 and news_count == 0:
            return []

        findings = [
            "Structured evidence assigned for this analysis: "
            f"{market_count} market item(s) and {news_count} news item(s)."
        ]
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
