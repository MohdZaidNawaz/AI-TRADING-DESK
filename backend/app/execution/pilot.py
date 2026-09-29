"""Hard-gated Pilot coordinator that remains disabled in Phase 3."""

from __future__ import annotations

from datetime import datetime

from app.models.proposals import DisabledPilotResult, SentinelDecision, TradeProposal
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry


class PilotApprovalError(ValueError):
    """Raised before Pilot can accept an invalid or unavailable approval."""


class PilotExecutionCoordinator:
    """Validate a Sentinel approval, consume it once, and perform no execution."""

    def __init__(self, approval_registry: ApprovalRegistry) -> None:
        self._approval_registry = approval_registry

    def prepare(
        self,
        *,
        proposal: TradeProposal,
        decision: SentinelDecision,
        now: datetime,
    ) -> DisabledPilotResult:
        """Accept only a registry-verified approval and always remain disabled."""
        try:
            verified = self._approval_registry.claim(
                proposal=proposal,
                decision=decision,
                now=now,
            )
        except ApprovalClaimError as exc:
            raise PilotApprovalError(str(exc)) from None

        return DisabledPilotResult(
            proposal_id=verified.proposal_id,
            approval_id=verified.approval_id,
            message="Pilot is disabled in Phase 3; no execution adapter was invoked.",
        )
