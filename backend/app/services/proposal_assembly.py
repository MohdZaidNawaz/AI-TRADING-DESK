"""Provider-neutral assembly of explicit proposals from a Master thesis."""

from __future__ import annotations

from app.evidence.models import EvidenceSnapshot, canonical_symbol
from app.models.events import AssetClass
from app.models.proposal_assembly import ExplicitTradeProposalInput
from app.models.proposals import (
    EvidenceReference,
    ObservedMarketReference,
    TradeProposal,
    TradeProposalStatus,
)
from app.models.trade_thesis import MasterTradeThesis, stable_fingerprint
from app.services.trade_proposal_workflow import build_evidence_bundle


class ProposalAssemblyError(ValueError):
    """Raised when explicit proposal input does not match its thesis snapshot."""


class ProposalAssemblyService:
    """Create an immutable pending proposal without inferring any parameters."""

    def assemble(
        self,
        *,
        master_thesis: MasterTradeThesis,
        snapshot: EvidenceSnapshot,
        analysis_id: str,
        proposal_input: ExplicitTradeProposalInput,
    ) -> TradeProposal:
        """Validate fixed lineage and copy only explicitly supplied trade values."""
        proposal_input = ExplicitTradeProposalInput.model_validate(proposal_input.model_dump())
        bundle = build_evidence_bundle(snapshot)
        thesis_reference = master_thesis.as_reference()
        if proposal_input.master_thesis_reference != thesis_reference:
            raise ProposalAssemblyError("Explicit input must reference the exact Master thesis.")
        if master_thesis.analysis_id != analysis_id:
            raise ProposalAssemblyError("Master thesis must match the captured analysis ID.")

        thesis_symbol = canonical_symbol(master_thesis.symbol)
        snapshot_symbol = canonical_symbol(snapshot.symbol)
        if thesis_symbol is None or snapshot_symbol is None or thesis_symbol != snapshot_symbol:
            raise ProposalAssemblyError("Master thesis symbol must exactly match the captured snapshot.")
        if master_thesis.evidence_fingerprint != bundle.fingerprint:
            raise ProposalAssemblyError("Master thesis must match the captured evidence snapshot.")
        if master_thesis.expires_at is not None and proposal_input.submitted_at >= master_thesis.expires_at:
            raise ProposalAssemblyError("Master thesis has expired for explicit proposal assembly.")

        observed_market_reference, asset_class = _observed_market_reference(
            snapshot=snapshot,
            references=bundle.references,
            symbol=thesis_symbol,
        )
        parameters = proposal_input.as_parameters()
        proposal_id = "explicit-proposal-" + stable_fingerprint(
            {
                "analysis_id": analysis_id,
                "master_thesis_reference": thesis_reference.model_dump(mode="json"),
                "evidence_fingerprint": bundle.fingerprint,
                "symbol": thesis_symbol,
                "input": proposal_input.model_dump(mode="json"),
            }
        )[:48]
        return TradeProposal(
            proposal_id=proposal_id,
            analysis_id=analysis_id,
            evidence_fingerprint=bundle.fingerprint,
            evidence_references=bundle.references,
            master_thesis_reference=thesis_reference,
            symbol=thesis_symbol,
            asset_class=asset_class,
            observed_market_reference=observed_market_reference,
            parameters=parameters,
            thesis=proposal_input.rationale,
            created_at=proposal_input.submitted_at,
            expires_at=proposal_input.expires_at,
            status=TradeProposalStatus.PENDING_RISK,
        )


def _observed_market_reference(
    *,
    snapshot: EvidenceSnapshot,
    references: tuple[EvidenceReference, ...],
    symbol: str,
) -> tuple[ObservedMarketReference | None, AssetClass]:
    """Attach a provenance-only observed price without deriving proposal values."""
    reference_by_event = {
        (reference.event_id, reference.source_id): reference for reference in references
    }
    candidates = sorted(
        (
            item
            for item in snapshot.market_evidence
            if canonical_symbol(item.symbol) == symbol
            and (item.price is not None or item.close is not None)
        ),
        key=lambda item: (
            item.timestamp.isoformat(),
            item.provenance.event_id,
            item.provenance.source_id,
        ),
    )
    if not candidates:
        return None, AssetClass.UNKNOWN
    evidence = candidates[-1]
    reference = reference_by_event.get((evidence.provenance.event_id, evidence.provenance.source_id))
    if reference is None:
        raise ProposalAssemblyError("Captured market evidence is missing required provenance.")
    return (
        ObservedMarketReference(
            evidence=reference,
            price=evidence.price if evidence.price is not None else evidence.close,
            observed_at=evidence.timestamp,
        ),
        evidence.asset_class,
    )
