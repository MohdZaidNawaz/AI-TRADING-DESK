"""Application-owned trusted Sentinel evaluation and durable issuance boundary.

This service accepts normal proposal, immutable evidence snapshot, and risk
context inputs.  It asks the configured Sentinel gate to derive a decision and
assessment, then persists only an internally derived APPROVED decision through
the durable approval facade.  It deliberately accepts no ``SentinelDecision``
from callers, and has no execution, adapter, MT5, or broker dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.evidence.models import EvidenceSnapshot
from app.models.proposals import RiskContext, SentinelDecision, SentinelDecisionStatus, TradeProposal
from app.models.risk_assessment import SentinelRiskAssessment

if TYPE_CHECKING:
    from app.services.trade_proposal_workflow import ApprovalRegistry, SentinelRiskGate


class TrustedSentinelIssuanceService:
    """Own the controlled application path from Sentinel evaluation to approval.

    This is an architectural trust boundary, not a claim that Python private
    methods protect against arbitrary hostile code in the same process. The
    composition root creates this service with the real Sentinel gate and the
    one configured durable approval facade; no public method takes a prebuilt
    decision as authority evidence.
    """

    def __init__(self, *, gate: "SentinelRiskGate", registry: "ApprovalRegistry") -> None:
        self._gate = gate
        self._registry = registry

    def evaluate_and_issue(
        self,
        *,
        proposal: TradeProposal,
        snapshot: EvidenceSnapshot,
        risk_context: RiskContext | None,
    ) -> tuple[SentinelDecision, SentinelRiskAssessment]:
        """Evaluate first; durably issue only a real APPROVED result.

        Rejected and insufficient-data decisions are returned unchanged and
        cannot create a durable approval record.
        """
        decision, assessment = self._gate._evaluate_unissued(
            proposal=proposal,
            snapshot=snapshot,
            risk_context=risk_context,
        )
        if decision.decision is SentinelDecisionStatus.APPROVED:
            decision = self._registry._persist_trusted_evaluation(proposal, decision)
        return decision, assessment
