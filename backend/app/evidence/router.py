"""Provider-neutral evidence routing for the nine FRIDAY specialist seats."""

from __future__ import annotations

from app.evidence.models import AgentEvidence, EvidenceProvenance, EvidenceSnapshot


NINE_SPECIALIST_IDS: tuple[str, ...] = (
    "atlas_macro",
    "capitol_smart_money",
    "scout_recon",
    "athena_analyst",
    "chartist_technician",
    "oracle_quant",
    "sentinel_risk_officer",
    "pilot_execution",
    "ledger_the_book",
)

_MARKET_AND_NEWS = frozenset(
    {
        "atlas_macro",
        "scout_recon",
        "sentinel_risk_officer",
        "pilot_execution",
        "ledger_the_book",
    }
)
_CONTEXTUAL_MARKET_AND_NEWS = frozenset(
    {"capitol_smart_money", "athena_analyst"}
)
_MARKET_ONLY = frozenset({"chartist_technician", "oracle_quant"})
_CONTEXTUAL_MARKET_LIMIT = 3


class EvidenceRouter:
    """Assign deterministic, provider-neutral evidence subsets to specialists."""

    def route(self, *, agent_id: str, snapshot: EvidenceSnapshot) -> AgentEvidence:
        if agent_id not in NINE_SPECIALIST_IDS:
            raise ValueError(f"Unknown FRIDAY specialist ID: {agent_id}")

        if agent_id in _MARKET_AND_NEWS:
            market_evidence = snapshot.market_evidence
            news_evidence = snapshot.news_evidence
        elif agent_id in _CONTEXTUAL_MARKET_AND_NEWS:
            market_evidence = snapshot.market_evidence[:_CONTEXTUAL_MARKET_LIMIT]
            news_evidence = snapshot.news_evidence
        elif agent_id in _MARKET_ONLY:
            market_evidence = snapshot.market_evidence
            news_evidence = ()
        else:  # pragma: no cover - guarded by NINE_SPECIALIST_IDS above
            raise ValueError(f"No evidence policy exists for specialist: {agent_id}")

        provenance: tuple[EvidenceProvenance, ...] = ()
        if agent_id == "ledger_the_book":
            provenance = tuple(
                evidence.provenance for evidence in market_evidence
            ) + tuple(evidence.provenance for evidence in news_evidence)

        return AgentEvidence(
            market_evidence=market_evidence,
            news_evidence=news_evidence,
            provenance=provenance,
        )
