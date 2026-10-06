"""Fail-closed structural validation before Pilot creates an execution intent."""
from __future__ import annotations
from datetime import datetime
from app.models.proposals import PositionSizingIntent,SentinelDecision,SentinelDecisionStatus,TradeProposal,TradeProposalStatus
from app.services.trade_proposal_workflow import proposal_fingerprint
def validate(proposal:TradeProposal,decision:SentinelDecision,now:datetime)->tuple[str,...]:
    reasons:list[str]=[]; p=proposal.parameters
    if proposal.status is not TradeProposalStatus.APPROVED: reasons.append("PROPOSAL_NOT_EXECUTABLE")
    if proposal.expires_at and now>=proposal.expires_at: reasons.append("PROPOSAL_EXPIRED")
    if proposal.symbol is None: reasons.append("MISSING_SYMBOL")
    if p is None or p.direction is None: reasons.append("MISSING_DIRECTION")
    if p is None or p.reference_price is None: reasons.append("MISSING_ENTRY")
    if p is None or p.stop_loss is None: reasons.append("MISSING_STOP")
    if p is None or not p.targets: reasons.append("MISSING_TARGET")
    if p is None or p.position_sizing_intent is PositionSizingIntent.UNSPECIFIED: reasons.append("MISSING_EXECUTION_PARAMETER")
    if p is not None and p.position_sizing_intent is PositionSizingIntent.RISK_FRACTION and p.requested_risk_fraction is None: reasons.append("MISSING_RISK_FRACTION")
    if decision.decision is not SentinelDecisionStatus.APPROVED or not decision.approval_id: reasons.append("APPROVAL_NOT_FOUND")
    if decision.proposal_id!=proposal.proposal_id: reasons.append("APPROVAL_PROPOSAL_MISMATCH")
    if decision.proposal_fingerprint!=proposal_fingerprint(proposal): reasons.append("APPROVAL_FINGERPRINT_MISMATCH")
    if decision.analysis_id!=proposal.analysis_id: reasons.append("APPROVAL_ANALYSIS_MISMATCH")
    if decision.evidence_fingerprint!=proposal.evidence_fingerprint: reasons.append("APPROVAL_EVIDENCE_MISMATCH")
    if not decision.account_id or not decision.portfolio_snapshot_id or not decision.portfolio_snapshot_fingerprint or not decision.metrics_snapshot_id or not decision.metrics_snapshot_fingerprint: reasons.append("ACCOUNT_BINDING_AMBIGUOUS")
    if decision.expires_at is None or now>=decision.expires_at: reasons.append("APPROVAL_EXPIRED")
    return tuple(sorted(set(reasons)))
