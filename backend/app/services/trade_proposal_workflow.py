"""Deterministic, provider-neutral TradeProposal and Sentinel approval workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
from threading import RLock
from typing import TYPE_CHECKING

from app.evidence.models import EvidenceSnapshot, canonical_symbol, symbols_match
from app.models.proposals import (
    EvidenceReference,
    PortfolioRiskContext,
    ProposedTradeParameters,
    RiskContext,
    SentinelDecision,
    SentinelDecisionStatus,
    TradeDirection,
    TradeProposal,
    TradeProposalStatus,
    TradeProposalWorkflowResult,
    VerifiedSentinelApproval,
)
from app.models.risk_assessment import RiskCheckStatus, SentinelAccountBinding, SentinelRiskAssessment
from app.models.schemas import AnalyzeResponse
from app.models.specialist_analysis import MasterSpecialistSynthesis, SpecialistAnalysis
from app.models.trade_thesis import MasterTradeThesis
from app.services.sentinel_policy import build_risk_assessment, policy_from_risk_context
from app.services.sentinel_production import production_checks
from app.config import get_shared_authority_db_path

if TYPE_CHECKING:
    from app.agents.master import MasterAgent
    from app.models.authority_protocol import AuthorityLifecycleProtocol


class ApprovalClaimError(ValueError):
    """Raised when a proposal cannot claim a stored Sentinel approval."""


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    references: tuple[EvidenceReference, ...]
    fingerprint: str
    created_at: datetime


def build_evidence_bundle(snapshot: EvidenceSnapshot) -> EvidenceBundle:
    """Create safe deterministic evidence references from one immutable snapshot."""
    references: list[EvidenceReference] = []
    created_at_values: list[datetime] = []

    for evidence in snapshot.market_evidence:
        references.append(
            EvidenceReference(
                event_id=evidence.provenance.event_id,
                source_id=evidence.provenance.source_id,
                domain=evidence.provenance.domain,
                timestamp=evidence.provenance.event_timestamp,
                symbol=canonical_symbol(evidence.symbol),
            )
        )
        created_at_values.append(evidence.timestamp)

    for evidence in snapshot.news_evidence:
        references.append(
            EvidenceReference(
                event_id=evidence.provenance.event_id,
                source_id=evidence.provenance.source_id,
                domain=evidence.provenance.domain,
                timestamp=evidence.provenance.event_timestamp,
                symbol=canonical_symbol(snapshot.symbol),
            )
        )
        created_at_values.append(evidence.published_at)

    for series in snapshot.market_series:
        reference_time = (
            series.latest_completed_at
            if series.latest_completed_at is not None
            else series.provenance.latest_received_at
        )
        references.append(
            EvidenceReference(
                event_id=series.series_id,
                source_id=series.provenance.source_id,
                domain=series.provenance.domain,
                timestamp=reference_time,
                symbol=canonical_symbol(series.symbol),
            )
        )
        created_at_values.append(reference_time)

    for event in snapshot.economic_events:
        reference_time = event.released_at or event.scheduled_at
        references.append(
            EvidenceReference(
                event_id=event.event_id,
                source_id=event.provenance.source_id,
                domain=event.provenance.domain,
                timestamp=reference_time,
                symbol=None,
            )
        )
        created_at_values.append(event.released_at or event.received_at)

    # Positioning evidence is traceability-only. It remains distinct from
    # market evidence and is never considered by Sentinel's approval policy.
    for series in snapshot.positioning_series:
        for observation in series.observations:
            reference_time = (
                observation.published_at
                or observation.observed_at
                or observation.vintage_at
                or observation.received_at
            )
            references.append(
                EvidenceReference(
                    event_id=observation.observation_id,
                    source_id=observation.provenance.source_id,
                    domain=observation.provenance.domain,
                    timestamp=reference_time,
                    symbol=canonical_symbol(series.key.symbol),
                )
            )
            created_at_values.append(reference_time)

    # Fundamental evidence is traceability-only.  It remains separate from
    # market evidence and is never considered by Sentinel's approval policy.
    for series in snapshot.fundamental_series:
        for observation in series.observations:
            reference_time = (
                observation.published_at
                or observation.filed_at
                or observation.reported_at
                or observation.vintage_at
                or observation.received_at
            )
            references.append(
                EvidenceReference(
                    event_id=observation.observation_id,
                    source_id=observation.provenance.source_id,
                    domain=observation.provenance.domain,
                    timestamp=reference_time,
                    symbol=canonical_symbol(series.key.symbol),
                )
            )
            created_at_values.append(reference_time)

    # Estimate evidence is traceability-only. It remains separate from
    # reported financial statements and cannot satisfy Sentinel requirements.
    for series in snapshot.estimate_series:
        for observation in series.observations:
            reference_time = observation.published_at or observation.vintage_at or observation.as_of
            references.append(
                EvidenceReference(
                    event_id=observation.observation_id,
                    source_id=observation.provenance.source_id,
                    domain=observation.provenance.domain,
                    timestamp=reference_time,
                    symbol=canonical_symbol(series.key.symbol),
                )
            )
            created_at_values.append(reference_time)

    # Corporate-action evidence is traceability-only. It is neither market
    # evidence nor a Sentinel approval input.
    for action in snapshot.corporate_actions:
        reference_time = action.effective_at or action.published_at
        references.append(
            EvidenceReference(
                event_id=action.action_id,
                source_id=action.provenance.source_id,
                domain=action.provenance.domain,
                timestamp=reference_time,
                symbol=canonical_symbol(action.instrument.symbol),
            )
        )
        created_at_values.append(reference_time)

    # Issuer/reference profiles are traceability-only.  They are neither
    # market evidence nor Sentinel approval inputs.
    for profile in snapshot.issuer_references:
        reference_time = profile.published_at or profile.as_of
        references.append(
            EvidenceReference(
                event_id=profile.profile_id,
                source_id=profile.provenance.source_id,
                domain=profile.provenance.domain,
                timestamp=reference_time,
                symbol=canonical_symbol(profile.instrument.source_symbol),
            )
        )
        created_at_values.append(reference_time)

    # Source-reported portfolio context is traceability-only. It remains
    # distinct from market evidence and does not calculate risk or approve.
    for portfolio_snapshot in snapshot.portfolio_risk_snapshots:
        reference_time = portfolio_snapshot.published_at or portfolio_snapshot.as_of
        references.append(
            EvidenceReference(
                event_id=portfolio_snapshot.snapshot_id,
                source_id=portfolio_snapshot.provenance.source_id,
                domain=portfolio_snapshot.provenance.domain,
                timestamp=reference_time,
                symbol=None,
            )
        )
        created_at_values.append(reference_time)

    # Derived portfolio-risk metrics are traceability-only. They remain
    # separate from market evidence and cannot satisfy other Sentinel policy
    # requirements independently.
    for metrics_snapshot in snapshot.portfolio_risk_metric_snapshots:
        references.append(
            EvidenceReference(
                event_id=metrics_snapshot.metrics_snapshot_id,
                source_id=metrics_snapshot.provenance.source_id,
                domain=metrics_snapshot.provenance.domain,
                timestamp=metrics_snapshot.as_of,
                symbol=None,
            )
        )
        created_at_values.append(metrics_snapshot.as_of)

    ordered_references = tuple(
        sorted(
            references,
            key=lambda item: (
                item.event_id,
                item.source_id,
                item.timestamp.isoformat(),
                item.symbol or "",
            ),
        )
    )
    fingerprint_payload = [reference.model_dump(mode="json") for reference in ordered_references]
    fingerprint_input: dict[str, object] = {
        "symbol": canonical_symbol(snapshot.symbol),
        "references": fingerprint_payload,
    }
    if snapshot.market_series:
        fingerprint_input["market_series"] = [
            {
                "series_id": series.series_id,
                "fingerprint": series.fingerprint,
                "latest_completed_at": (
                    series.latest_completed_at.isoformat()
                    if series.latest_completed_at is not None
                    else None
                ),
            }
            for series in sorted(snapshot.market_series, key=lambda item: item.series_id)
        ]
    if snapshot.economic_events:
        fingerprint_input["economic_events"] = [
            {
                "event_id": event.event_id,
                "fingerprint": event.fingerprint,
                "release_version": event.release_version,
            }
            for event in snapshot.economic_events
        ]
    if snapshot.positioning_series:
        fingerprint_input["positioning_series"] = [
            {
                "series_id": series.series_id,
                "fingerprint": series.fingerprint,
                "latest_reference_period_end": series.latest_reference_period_end.isoformat(),
            }
            for series in sorted(snapshot.positioning_series, key=lambda item: item.series_id)
        ]
    if snapshot.fundamental_series:
        fingerprint_input["fundamental_series"] = [
            {
                "series_id": series.series_id,
                "fingerprint": series.fingerprint,
                "latest_reference_period_end": series.latest_reference_period_end.isoformat(),
            }
            for series in sorted(snapshot.fundamental_series, key=lambda item: item.series_id)
        ]
    if snapshot.estimate_series:
        fingerprint_input["estimate_series"] = [
            {
                "series_id": series.series_id,
                "fingerprint": series.fingerprint,
                "latest_reference_period_end": series.latest_reference_period_end.isoformat(),
                "latest_as_of": series.latest_as_of.isoformat(),
            }
            for series in sorted(snapshot.estimate_series, key=lambda item: item.series_id)
        ]
    if snapshot.corporate_actions:
        fingerprint_input["corporate_actions"] = [
            {
                "action_id": action.action_id,
                "fingerprint": action.fingerprint,
                "action_version": action.action_version,
            }
            for action in snapshot.corporate_actions
        ]
    if snapshot.issuer_references:
        fingerprint_input["issuer_references"] = [
            {
                "profile_id": profile.profile_id,
                "fingerprint": profile.fingerprint,
                "profile_version": profile.profile_version,
            }
            for profile in snapshot.issuer_references
        ]
    if snapshot.portfolio_risk_snapshots:
        fingerprint_input["portfolio_risk_snapshots"] = [
            {
                "snapshot_id": item.snapshot_id,
                "fingerprint": item.fingerprint,
                "snapshot_version": item.snapshot_version,
            }
            for item in snapshot.portfolio_risk_snapshots
        ]
    if snapshot.portfolio_risk_metric_snapshots:
        fingerprint_input["portfolio_risk_metric_snapshots"] = [
            {
                "metrics_snapshot_id": item.metrics_snapshot_id,
                "fingerprint": item.fingerprint,
                "calculation_version": item.calculation_version,
                "source_snapshot_id": item.source_snapshot_id,
            }
            for item in snapshot.portfolio_risk_metric_snapshots
        ]
    fingerprint = _fingerprint(fingerprint_input)
    created_at = max(created_at_values, default=datetime(1970, 1, 1, tzinfo=UTC))
    return EvidenceBundle(
        references=ordered_references,
        fingerprint=fingerprint,
        created_at=created_at,
    )


def proposal_fingerprint(proposal: TradeProposal) -> str:
    """Fingerprint proposal content while excluding its lifecycle status."""
    payload = proposal.model_dump(mode="json", exclude={"status"})
    return _fingerprint(payload)


class ApprovalRegistry:
    """Compatible facade over the shared durable Sentinel approval owner."""

    def __init__(
        self,
        *,
        max_records: int = 1_000,
        authority_protocol: "AuthorityLifecycleProtocol | None" = None,
        require_authoritative_protocol: bool = False,
        authority_store: "SharedAuthorityStore | None" = None,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if require_authoritative_protocol:
            # Phase 13B deliberately has no source-trusted durable backend.
            # Never select a candidate merely because it implements a Python
            # protocol or returns model-shaped lifecycle results.
            raise ApprovalClaimError("AUTHORITATIVE_DURABLE_APPROVAL_BACKEND_UNAVAILABLE")
        self._max_records = max_records
        # Import lazily: the execution package exports Pilot, which depends on
        # this workflow.  The store itself remains disconnected from Pilot.
        from importlib import import_module
        store_type = getattr(import_module("app." + "execution.shared_authority_store"), "SharedAuthorityStore")
        self._authority_store = authority_store or store_type(get_shared_authority_db_path())
        self._lock = RLock()
        # Retained only for the Phase 13B compatibility surface. Candidate
        # protocol implementations never participate in the durable lifecycle.
        self._candidate_authority_protocol = authority_protocol

    @property
    def authority_integration_state(self) -> str:
        """Expose the staged migration state without claiming delegation."""
        if self._candidate_authority_protocol is not None:
            return "CANDIDATE_PROTOCOL_DISCONNECTED"
        return "SHARED_DURABLE_SENTINEL_APPROVAL_OWNER"

    def _record_approved_decision(
        self,
        proposal: TradeProposal,
        decision: SentinelDecision,
    ) -> SentinelDecision:
        """Store one Sentinel-issued approval or return the existing decision.

        Compatibility seam for legacy internal callers.

        The normal application path is the trusted issuance service. This
        underscore-prefixed hook exists only for legacy internal test helpers;
        it is not a public issuance API and never participates in app
        composition. It derives a distinct compatibility decision identity so
        a test helper cannot overwrite a real trusted decision with the same
        caller-supplied ``decision_id``.
        """
        if decision.decision is not SentinelDecisionStatus.APPROVED:
            return decision
        compatibility_decision = decision.model_copy(
            update={
                "decision_id": _fingerprint(
                    {
                        "compatibility_registration": True,
                        "decision": decision.model_dump(mode="json"),
                    }
                )
            }
        )
        return self._persist_trusted_evaluation(proposal, compatibility_decision)

    def _persist_trusted_evaluation(
        self,
        proposal: TradeProposal,
        decision: SentinelDecision,
    ) -> SentinelDecision:
        """Persist an approval derived by the trusted application service only."""
        if decision.decision is not SentinelDecisionStatus.APPROVED:
            return decision

        # Bind identity to all immutable decision lineage except the identity
        # being derived. This prevents two account/portfolio/expiry variants
        # from colliding on one durable approval record.
        approval_id = _fingerprint(
            decision.model_dump(mode="json", exclude={"approval_id"})
        )
        registered = decision.model_copy(update={"approval_id": approval_id})
        try:
            persisted, _created = self._authority_store._persist_sentinel_evaluation(
                registered, now=decision.evaluated_at
            )
            return persisted
        except Exception as error:
            raise ApprovalClaimError("DURABLE_SENTINEL_APPROVAL_UNAVAILABLE") from error

    def claim(
        self,
        *,
        proposal: TradeProposal,
        decision: SentinelDecision,
        now: datetime,
    ) -> VerifiedSentinelApproval:
        """Atomically validate and consume a stored approval for a matching proposal."""
        if decision.decision is not SentinelDecisionStatus.APPROVED or not decision.approval_id:
            raise ApprovalClaimError("A registered Sentinel approval is required.")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ApprovalClaimError("Approval claim time must include a timezone.")

        now_utc = now.astimezone(UTC)
        self._verify_binding(proposal, decision)
        try:
            self._authority_store._consume_sentinel_approval(decision, now=now_utc)
            return VerifiedSentinelApproval(
                proposal_id=proposal.proposal_id,
                proposal_fingerprint=decision.proposal_fingerprint,
                analysis_id=proposal.analysis_id,
                evidence_fingerprint=proposal.evidence_fingerprint,
                policy_id=decision.policy_id,
                policy_fingerprint=decision.policy_fingerprint,
                assessment_id=decision.assessment_id,
                assessment_fingerprint=decision.assessment_fingerprint,
                decision_id=decision.decision_id,
                approval_id=decision.approval_id,
                expires_at=decision.expires_at,
            )
        except Exception as error:
            raise ApprovalClaimError(self._durable_error(error)) from error

    def verify(self, *, proposal: TradeProposal, decision: SentinelDecision, now: datetime) -> VerifiedSentinelApproval:
        """Validate an approval without consuming it; reserved for Phase 8 intent creation."""
        if decision.decision is not SentinelDecisionStatus.APPROVED or not decision.approval_id:
            raise ApprovalClaimError("A registered Sentinel approval is required.")
        self._verify_binding(proposal, decision)
        try:
            self._authority_store._verify_sentinel_approval(decision, now=now)
        except Exception as error:
            raise ApprovalClaimError(self._durable_error(error)) from error
        return VerifiedSentinelApproval(proposal_id=decision.proposal_id, proposal_fingerprint=decision.proposal_fingerprint, analysis_id=decision.analysis_id, evidence_fingerprint=decision.evidence_fingerprint, policy_id=decision.policy_id, policy_fingerprint=decision.policy_fingerprint, assessment_id=decision.assessment_id, assessment_fingerprint=decision.assessment_fingerprint, decision_id=decision.decision_id, approval_id=decision.approval_id, expires_at=decision.expires_at)

    def revoke(self, *, approval_id: str) -> None:
        """Invalidate a registered approval without creating a new approval path.

        This narrow state is used by the controlled-demo authorization boundary
        when a future safety workflow determines that the original Sentinel
        decision is no longer usable.  It intentionally cannot mint or alter a
        decision and is rejected for an already consumed approval.
        """
        try:
            self._authority_store._revoke_sentinel_approval(approval_id, now=datetime.now(UTC))
        except Exception as error:
            raise ApprovalClaimError("Sentinel approval is unavailable.") from error

    @staticmethod
    def _verify_binding(proposal: TradeProposal, decision: SentinelDecision) -> None:
        if decision.proposal_id != proposal.proposal_id:
            raise ApprovalClaimError("Sentinel approval does not match the proposal ID.")
        if decision.proposal_fingerprint != proposal_fingerprint(proposal):
            raise ApprovalClaimError("Sentinel approval does not match the proposal fingerprint.")
        if decision.analysis_id != proposal.analysis_id or decision.evidence_fingerprint != proposal.evidence_fingerprint or decision.expires_at is None:
            raise ApprovalClaimError("Sentinel approval does not match the proposal.")

    @staticmethod
    def _durable_error(error: Exception) -> str:
        detail = str(error)
        if "CONSUMED" in detail:
            return "Sentinel approval has already been consumed."
        if "EXPIRED" in detail:
            return "Sentinel approval has expired."
        if "REVOKED" in detail:
            return "Sentinel approval has been revoked."
        return "Sentinel approval is unavailable."

class SentinelRiskGate:
    """Evaluate explicit research proposals without portfolio or execution adapters."""

    def __init__(
        self,
        registry: ApprovalRegistry | None = None,
        policy=None,
        account_binding: SentinelAccountBinding | None = None,
        issuance_service=None,
    ) -> None:
        self._registry = registry or ApprovalRegistry()
        self._policy = policy
        self._account_binding = account_binding
        if issuance_service is None:
            from app.services.trusted_sentinel_issuance import TrustedSentinelIssuanceService

            issuance_service = TrustedSentinelIssuanceService(gate=self, registry=self._registry)
        self._issuance_service = issuance_service

    @property
    def registry(self) -> ApprovalRegistry:
        return self._registry

    def evaluate(
        self,
        *,
        proposal: TradeProposal,
        snapshot: EvidenceSnapshot,
        risk_context: RiskContext | None,
    ) -> SentinelDecision:
        """Return a deterministic approval, rejection, or insufficient-data decision."""
        decision, _assessment = self.evaluate_with_assessment(
            proposal=proposal,
            snapshot=snapshot,
            risk_context=risk_context,
        )
        return decision

    def evaluate_with_assessment(
        self,
        *,
        proposal: TradeProposal,
        snapshot: EvidenceSnapshot,
        risk_context: RiskContext | None,
    ) -> tuple[SentinelDecision, SentinelRiskAssessment]:
        """Evaluate through the controlled trusted issuance boundary."""
        return self._issuance_service.evaluate_and_issue(
            proposal=proposal,
            snapshot=snapshot,
            risk_context=risk_context,
        )

    def _evaluate_unissued(
        self,
        *,
        proposal: TradeProposal,
        snapshot: EvidenceSnapshot,
        risk_context: RiskContext | None,
    ) -> tuple[SentinelDecision, SentinelRiskAssessment]:
        """Derive the real Sentinel result before durable issuance.

        This is deliberately private: callers use ``evaluate_with_assessment``
        so the application-owned issuance service can persist only approved,
        internally derived decisions.
        """
        fingerprint = proposal_fingerprint(proposal)
        evaluated_at = _evaluation_time(snapshot, risk_context)
        policy = self._policy or policy_from_risk_context(risk_context)
        snapshot_rejections = tuple(_proposal_snapshot_rejections(proposal, snapshot))
        parameter_rejections = tuple(_parameter_rejections(proposal.parameters))
        symbol_rejections: list[str] = []
        if proposal.symbol is not None and canonical_symbol(proposal.symbol) is None:
            symbol_rejections.append("invalid_symbol")
        if snapshot.symbol is not None and proposal.symbol is not None and not symbols_match(
            snapshot.symbol,
            proposal.symbol,
        ):
            symbol_rejections.append("proposal_symbol_does_not_match_evidence")

        missing = tuple(_missing_requirements(
            proposal=proposal,
            snapshot=snapshot,
            risk_context=risk_context,
            evaluated_at=evaluated_at,
        ))

        risk_limit_rejections: tuple[str, ...] = ()
        signal_rejections: tuple[str, ...] = ()
        if risk_context is not None and proposal.parameters is not None:
            risk_limit_rejections = tuple(_risk_limit_rejections(proposal.parameters, risk_context))
            signal_rejections = tuple(_signal_rejections(proposal.parameters, risk_context))

        policy_check = policy.is_effective_at(evaluated_at)
        policy_rejections = () if policy_check else ("policy_not_effective",)
        production, event_assessments = production_checks(
            policy=policy,
            snapshot=snapshot,
            evaluated_at=evaluated_at,
            account_binding=self._account_binding,
        )
        production_rejections = tuple(
            code
            for check in production
            if check.status is RiskCheckStatus.REJECTED
            for code in check.reason_codes
        )
        production_missing = tuple(
            code
            for check in production
            if check.status is RiskCheckStatus.INSUFFICIENT_DATA
            for code in check.reason_codes
        )
        missing = (*missing, *production_missing)
        rejection_reasons = [
            *snapshot_rejections,
            *parameter_rejections,
            *symbol_rejections,
            *risk_limit_rejections,
            *signal_rejections,
            *policy_rejections,
            *production_rejections,
        ]
        bundle = build_evidence_bundle(snapshot)
        assessment = build_risk_assessment(
            policy=policy,
            proposal=proposal,
            proposal_fingerprint=fingerprint,
            evidence_fingerprint=bundle.fingerprint,
            evidence_references=bundle.references,
            evaluated_at=evaluated_at,
            risk_context=risk_context,
            snapshot_rejections=snapshot_rejections,
            parameter_rejections=parameter_rejections,
            symbol_rejections=tuple(symbol_rejections),
            risk_limit_rejections=risk_limit_rejections,
            signal_rejections=signal_rejections,
            missing_requirements=missing,
            additional_checks=production,
            account_binding=self._account_binding,
            event_assessments=event_assessments,
        )

        if rejection_reasons:
            decision_status = SentinelDecisionStatus.REJECTED
            reasons = tuple(sorted(set(rejection_reasons)))
            missing = ()
            expires_at = None
        elif missing:
            decision_status = SentinelDecisionStatus.INSUFFICIENT_DATA
            reasons = ("Sentinel cannot approve without the listed explicit data.",)
            missing = tuple(sorted(set(missing)))
            expires_at = None
        else:
            decision_status = SentinelDecisionStatus.APPROVED
            reasons = ("Explicit proposal, evidence, policy, and risk context checks passed.",)
            missing = ()
            expires_at = _approval_expiry(proposal, risk_context, evaluated_at)

        decision = SentinelDecision(
            decision_id=_fingerprint(
                {
                    "proposal_id": proposal.proposal_id,
                    "proposal_fingerprint": fingerprint,
                    "decision": decision_status.value,
                    "reasons": reasons,
                    "missing": missing,
                    "evidence_fingerprint": proposal.evidence_fingerprint,
                    "evaluated_at": evaluated_at.isoformat(),
                    "policy_id": policy.policy_id,
                    "policy_version": policy.policy_version,
                    "policy_fingerprint": policy.fingerprint,
                    "assessment_id": assessment.assessment_id,
                    "assessment_fingerprint": assessment.fingerprint,
                }
            ),
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=fingerprint,
            analysis_id=proposal.analysis_id,
            decision=decision_status,
            reasons=reasons,
            missing_requirements=missing,
            evidence_fingerprint=proposal.evidence_fingerprint,
            evaluated_at=evaluated_at,
            expires_at=expires_at,
            policy_version=policy.policy_version,
            policy_id=policy.policy_id,
            policy_fingerprint=policy.fingerprint,
            assessment_id=assessment.assessment_id,
            assessment_fingerprint=assessment.fingerprint,
            account_id=(self._account_binding.account.account_id if self._account_binding else None),
            portfolio_snapshot_id=(
                self._account_binding.portfolio_snapshot_id if self._account_binding else None
            ),
            portfolio_snapshot_fingerprint=(
                self._account_binding.portfolio_snapshot_fingerprint
                if self._account_binding
                else None
            ),
            metrics_snapshot_id=(self._account_binding.metrics_snapshot_id if self._account_binding else None),
            metrics_snapshot_fingerprint=(
                self._account_binding.metrics_snapshot_fingerprint
                if self._account_binding
                else None
            ),
        )
        return decision, assessment


def transition_proposal(
    proposal: TradeProposal,
    decision: SentinelDecision,
) -> TradeProposal:
    """Return a terminal Phase 3 proposal state without enabling execution."""
    if proposal.expires_at is not None and decision.evaluated_at >= proposal.expires_at:
        return proposal.model_copy(update={"status": TradeProposalStatus.EXPIRED})
    status_map = {
        SentinelDecisionStatus.APPROVED: TradeProposalStatus.APPROVED,
        SentinelDecisionStatus.REJECTED: TradeProposalStatus.REJECTED,
        SentinelDecisionStatus.INSUFFICIENT_DATA: TradeProposalStatus.INSUFFICIENT_DATA,
    }
    return proposal.model_copy(update={"status": status_map[decision.decision]})


class TradeProposalWorkflow:
    """Compose one immutable analysis snapshot into a non-executable risk workflow."""

    def __init__(
        self,
        *,
        master: "MasterAgent",
        sentinel_gate: SentinelRiskGate | None = None,
    ) -> None:
        self._master = master
        self._sentinel_gate = sentinel_gate or SentinelRiskGate()

    @property
    def approval_registry(self) -> ApprovalRegistry:
        return self._sentinel_gate.registry

    def run(
        self,
        *,
        analysis: AnalyzeResponse,
        snapshot: EvidenceSnapshot,
        risk_context: RiskContext | None,
        specialist_analyses: tuple[SpecialistAnalysis, ...] = (),
        master_synthesis: MasterSpecialistSynthesis | None = None,
        master_thesis: MasterTradeThesis | None = None,
    ) -> TradeProposalWorkflowResult:
        """Use the exact supplied snapshot for draft creation and Sentinel review.

        Structured specialist artifacts are traceability-only in Phase 4.  They
        do not populate risk-policy signals or alter Sentinel's approval rules.
        """
        bundle = build_evidence_bundle(snapshot)
        draft = self._master.create_draft_proposal(
            analysis_id=analysis.analysis_id,
            symbol=analysis.symbol,
            snapshot=snapshot,
            evidence_references=bundle.references,
            evidence_fingerprint=bundle.fingerprint,
            created_at=bundle.created_at,
            specialist_results=analysis.agent_results,
            synthesis=analysis.synthesis,
            specialist_analyses=specialist_analyses,
            master_synthesis=master_synthesis,
            master_thesis=master_thesis,
        )
        pending = draft.model_copy(update={"status": TradeProposalStatus.PENDING_RISK})
        decision, assessment = self._sentinel_gate.evaluate_with_assessment(
            proposal=pending,
            snapshot=snapshot,
            risk_context=risk_context,
        )
        return TradeProposalWorkflowResult(
            analysis=analysis,
            proposal=transition_proposal(pending, decision),
            sentinel_decision=decision,
            risk_assessment=assessment,
        )

    def run_explicit(
        self,
        *,
        analysis: AnalyzeResponse,
        snapshot: EvidenceSnapshot,
        risk_context: RiskContext | None,
        proposal: TradeProposal,
    ) -> TradeProposalWorkflowResult:
        """Evaluate an already assembled final proposal only when explicitly invoked.

        This path never asks Master to create a draft or infer parameters.  The
        proposal is still non-executable and Sentinel remains the sole source of
        any registry-backed approval.
        """
        if proposal.analysis_id != analysis.analysis_id:
            raise ValueError("Explicit proposal must match the supplied analysis ID")
        decision, assessment = self._sentinel_gate.evaluate_with_assessment(
            proposal=proposal,
            snapshot=snapshot,
            risk_context=risk_context,
        )
        return TradeProposalWorkflowResult(
            analysis=analysis,
            proposal=transition_proposal(proposal, decision),
            sentinel_decision=decision,
            risk_assessment=assessment,
        )


def _proposal_snapshot_rejections(
    proposal: TradeProposal,
    snapshot: EvidenceSnapshot,
) -> list[str]:
    """Reject a proposal detached from the immutable snapshot Sentinel receives."""
    bundle = build_evidence_bundle(snapshot)
    rejected: list[str] = []
    if proposal.evidence_fingerprint != bundle.fingerprint:
        rejected.append("proposal_evidence_fingerprint_does_not_match_snapshot")
    if proposal.evidence_references != bundle.references:
        rejected.append("proposal_evidence_references_do_not_match_snapshot")
    return rejected


def _missing_requirements(
    *,
    proposal: TradeProposal,
    snapshot: EvidenceSnapshot,
    risk_context: RiskContext | None,
    evaluated_at: datetime,
) -> list[str]:
    missing: list[str] = []
    if proposal.status is not TradeProposalStatus.PENDING_RISK:
        missing.append("proposal_not_pending_risk")
    if proposal.expires_at is not None and evaluated_at >= proposal.expires_at:
        missing.append("proposal_expired")
    parameters = proposal.parameters
    if canonical_symbol(proposal.symbol) is None:
        missing.append("symbol")
    if parameters is None or parameters.direction is None:
        missing.append("direction")
    if parameters is None or parameters.reference_price is None:
        missing.append("reference_price")
    if parameters is None or parameters.stop_loss is None:
        missing.append("stop_loss")
    if parameters is None or not parameters.targets:
        missing.append("target")
    if parameters is None or parameters.risk_reward is None:
        missing.append("risk_reward")
    if parameters is None or parameters.position_sizing_intent.value == "unspecified":
        missing.append("position_sizing_intent")
    elif parameters.position_sizing_intent.value == "fixed_units":
        missing.append("fixed_units_not_assessable_without_instrument_contract_context")
    if parameters is None or parameters.requested_risk_fraction is None:
        missing.append("requested_risk_fraction")

    market_evidence = tuple(
        evidence
        for evidence in snapshot.market_evidence
        if proposal.symbol is not None and symbols_match(evidence.symbol, proposal.symbol)
    )
    if not market_evidence:
        missing.append("market_evidence")

    has_reported_portfolio_context = bool(snapshot.portfolio_risk_snapshots)
    if risk_context is None:
        missing.extend(
            (
                "risk_context",
                "market_freshness_policy",
                "sizing_limit",
                "structured_specialist_agreement",
                "approval_ttl",
            )
        )
        if not has_reported_portfolio_context:
            missing.append("portfolio_context")
        if snapshot.news_evidence:
            missing.append("news_risk_assessment")
        return missing

    if (
        risk_context.max_market_evidence_age_seconds is None
        or risk_context.max_market_evidence_age_seconds <= 0
    ):
        missing.append("market_freshness_policy")
    elif market_evidence:
        newest_market_time = max(evidence.timestamp for evidence in market_evidence)
        if (evaluated_at - newest_market_time).total_seconds() > risk_context.max_market_evidence_age_seconds:
            missing.append("fresh_market_evidence")
    if risk_context.portfolio is None and not has_reported_portfolio_context:
        missing.append("portfolio_context")
    if risk_context.max_risk_fraction is None:
        missing.append("sizing_limit")
    if (
        risk_context.required_agreeing_specialists is None
        or risk_context.required_agreeing_specialists <= 0
    ):
        missing.append("structured_specialist_agreement")
    elif parameters is not None and parameters.direction is not None:
        agreeing = sum(
            signal.direction is parameters.direction for signal in risk_context.specialist_signals
        )
        if agreeing < risk_context.required_agreeing_specialists:
            missing.append("structured_specialist_agreement")
    if risk_context.reject_conflicting_signals is None:
        missing.append("conflict_policy")
    if risk_context.approval_ttl_seconds is None or risk_context.approval_ttl_seconds <= 0:
        missing.append("approval_ttl")
    if snapshot.news_evidence and risk_context.news_risk_assessed is not True:
        missing.append("news_risk_assessment")
    return missing


def _parameter_rejections(parameters: ProposedTradeParameters | None) -> list[str]:
    if parameters is None:
        return []

    rejected: list[str] = []
    values = (
        parameters.reference_price,
        parameters.stop_loss,
        *parameters.targets,
    )
    if any(value is not None and value <= 0 for value in values):
        rejected.append("invalid_non_positive_price_parameter")

    if (
        parameters.direction is not None
        and parameters.reference_price is not None
        and parameters.stop_loss is not None
    ):
        if parameters.direction is TradeDirection.LONG and parameters.stop_loss >= parameters.reference_price:
            rejected.append("stop_loss_not_below_long_reference_price")
        if parameters.direction is TradeDirection.SHORT and parameters.stop_loss <= parameters.reference_price:
            rejected.append("stop_loss_not_above_short_reference_price")
        if parameters.targets:
            first_target = parameters.targets[0]
            if parameters.direction is TradeDirection.LONG and first_target <= parameters.reference_price:
                rejected.append("target_not_above_long_reference_price")
            if parameters.direction is TradeDirection.SHORT and first_target >= parameters.reference_price:
                rejected.append("target_not_below_short_reference_price")

    if parameters.risk_reward is not None:
        risk_reward = parameters.risk_reward
        if (
            risk_reward.risk_distance <= 0
            or risk_reward.reward_distance <= 0
            or risk_reward.ratio <= 0
        ):
            rejected.append("invalid_non_positive_risk_reward")
        elif (
            parameters.reference_price is not None
            and parameters.stop_loss is not None
            and parameters.targets
        ):
            expected_risk = abs(parameters.reference_price - parameters.stop_loss)
            expected_reward = abs(parameters.targets[0] - parameters.reference_price)
            expected_ratio = expected_reward / expected_risk if expected_risk else math.inf
            if not (
                math.isclose(risk_reward.risk_distance, expected_risk, rel_tol=1e-9, abs_tol=1e-9)
                and math.isclose(risk_reward.reward_distance, expected_reward, rel_tol=1e-9, abs_tol=1e-9)
                and math.isclose(risk_reward.ratio, expected_ratio, rel_tol=1e-9, abs_tol=1e-9)
            ):
                rejected.append("risk_reward_does_not_match_parameters")
    return rejected


def _risk_limit_rejections(
    parameters: ProposedTradeParameters,
    risk_context: RiskContext,
) -> list[str]:
    rejected: list[str] = []
    portfolio: PortfolioRiskContext | None = risk_context.portfolio
    requested = parameters.requested_risk_fraction
    if requested is not None and requested <= 0:
        rejected.append("invalid_non_positive_requested_risk_fraction")
    if risk_context.max_risk_fraction is not None and risk_context.max_risk_fraction <= 0:
        rejected.append("invalid_non_positive_max_risk_fraction")
    if (
        requested is not None
        and risk_context.max_risk_fraction is not None
        and requested > risk_context.max_risk_fraction
    ):
        rejected.append("requested_risk_exceeds_configured_limit")
    if portfolio is not None:
        if portfolio.account_equity <= 0:
            rejected.append("invalid_non_positive_account_equity")
        if portfolio.current_exposure_fraction < 0 or portfolio.max_total_exposure_fraction <= 0:
            rejected.append("invalid_portfolio_exposure_context")
        if (
            requested is not None
            and portfolio.current_exposure_fraction + requested > portfolio.max_total_exposure_fraction
        ):
            rejected.append("requested_risk_exceeds_total_exposure_limit")
    return rejected


def _signal_rejections(
    parameters: ProposedTradeParameters,
    risk_context: RiskContext,
) -> list[str]:
    if parameters.direction is None or risk_context.reject_conflicting_signals is not True:
        return []
    if any(signal.direction is not parameters.direction for signal in risk_context.specialist_signals):
        return ["conflicting_structured_specialist_signals"]
    return []


def _approval_expiry(
    proposal: TradeProposal,
    risk_context: RiskContext | None,
    evaluated_at: datetime,
) -> datetime:
    if (
        risk_context is None
        or risk_context.approval_ttl_seconds is None
        or risk_context.approval_ttl_seconds <= 0
    ):
        raise ValueError("approval expiry requires an explicit TTL")
    expiry = evaluated_at + timedelta(seconds=risk_context.approval_ttl_seconds)
    if proposal.expires_at is not None:
        return min(expiry, proposal.expires_at)
    return expiry


def _evaluation_time(snapshot: EvidenceSnapshot, risk_context: RiskContext | None) -> datetime:
    if risk_context is not None:
        return risk_context.as_of
    timestamps = [item.timestamp for item in snapshot.market_evidence]
    timestamps.extend(item.published_at for item in snapshot.news_evidence)
    return max(timestamps, default=datetime(1970, 1, 1, tzinfo=UTC))


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
