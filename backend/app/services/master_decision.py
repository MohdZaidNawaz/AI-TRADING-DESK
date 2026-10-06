"""Deterministic construction of a non-authorizing Master trade thesis."""

from __future__ import annotations

from datetime import datetime

from app.evidence.models import canonical_symbol
from app.models.specialist_analysis import (
    MasterSpecialistSynthesis,
    SpecialistAnalysis,
    SpecialistAnalysisStatus,
    SpecialistConsensus,
    SpecialistStance,
)
from app.models.trade_thesis import (
    MasterTradeThesis,
    ThesisAnalyticalBias,
    ThesisConsensusState,
    ThesisEvidenceCoverage,
    ThesisSpecialistArtifactReference,
    stable_fingerprint,
    thesis_identity,
)


class MasterDecisionService:
    """Build one deterministic thesis from immutable specialist artifacts only."""

    def build(
        self,
        *,
        analysis_id: str,
        symbol: str | None,
        evidence_fingerprint: str,
        evidence_as_of: datetime,
        master_synthesis: MasterSpecialistSynthesis,
    ) -> MasterTradeThesis:
        if not isinstance(master_synthesis, MasterSpecialistSynthesis):
            raise TypeError("master_synthesis must be a MasterSpecialistSynthesis")
        if master_synthesis.analysis_id != analysis_id:
            raise ValueError("Master synthesis must match the thesis analysis ID")
        if master_synthesis.evidence_fingerprint != evidence_fingerprint:
            raise ValueError("Master synthesis must match the thesis evidence fingerprint")

        analyses = tuple(
            sorted(
                master_synthesis.specialist_analyses,
                key=lambda item: (item.agent_id, item.analysis_id),
            )
        )
        self._validate_specialist_artifact_set(master_synthesis, analyses)
        synthesis_fingerprint = stable_fingerprint(master_synthesis.model_dump(mode="json"))
        consensus = ThesisConsensusState(master_synthesis.consensus.value)
        bias = _analytical_bias(master_synthesis.consensus, analyses)
        limitations = _limitations(master_synthesis.consensus, analyses, bias)
        invalidating_conditions = tuple(
            sorted(
                {
                    condition
                    for analysis in analyses
                    for condition in analysis.invalidating_conditions
                    if condition.strip()
                }
            )
        )
        references = tuple(master_synthesis.supporting_evidence)
        artifact_references = tuple(
            ThesisSpecialistArtifactReference(
                agent_id=analysis.agent_id,
                analysis_id=analysis.analysis_id,
                analysis_fingerprint=analysis.analysis_fingerprint,
                status=analysis.status,
                stance=analysis.stance,
            )
            for analysis in analyses
        )
        coverage = ThesisEvidenceCoverage(
            referenced_evidence_count=len(references),
            evidence_domains=tuple(reference.domain for reference in references),
            specialist_artifact_count=len(analyses),
            completed_specialist_count=sum(
                item.status is SpecialistAnalysisStatus.COMPLETED for item in analyses
            ),
            insufficient_specialist_count=len(master_synthesis.insufficient_data_analysis_ids),
        )
        thesis_id = thesis_identity(
            analysis_id=analysis_id,
            evidence_fingerprint=evidence_fingerprint,
            master_synthesis_id=master_synthesis.synthesis_id,
            master_synthesis_fingerprint=synthesis_fingerprint,
        )
        return MasterTradeThesis(
            thesis_id=thesis_id,
            analysis_id=analysis_id,
            symbol=canonical_symbol(symbol),
            evidence_fingerprint=evidence_fingerprint,
            evidence_references=references,
            master_synthesis_id=master_synthesis.synthesis_id,
            master_synthesis_fingerprint=synthesis_fingerprint,
            specialist_artifact_references=artifact_references,
            analytical_bias=bias,
            consensus_state=consensus,
            evidence_coverage=coverage,
            evidence_as_of=evidence_as_of,
            limitations=limitations,
            invalidating_conditions=invalidating_conditions,
            created_at=evidence_as_of,
        )

    @staticmethod
    def _validate_specialist_artifact_set(
        synthesis: MasterSpecialistSynthesis,
        analyses: tuple[SpecialistAnalysis, ...],
    ) -> None:
        if tuple(item.analysis_id for item in analyses) != tuple(
            item.analysis_id for item in synthesis.specialist_analyses
        ):
            raise ValueError("Master synthesis specialist artifacts must be deterministically ordered")
        if any(
            item not in {analysis.analysis_id for analysis in analyses}
            for item in synthesis.participating_analysis_ids
        ):
            raise ValueError("Master synthesis participation references must match specialist artifacts")


def _analytical_bias(
    consensus: SpecialistConsensus,
    analyses: tuple[SpecialistAnalysis, ...],
) -> ThesisAnalyticalBias:
    if consensus is SpecialistConsensus.CONFLICTED:
        return ThesisAnalyticalBias.CONFLICTED
    if consensus is not SpecialistConsensus.AGREEMENT:
        return ThesisAnalyticalBias.INSUFFICIENT_DATA
    directional = {
        item.stance
        for item in analyses
        if item.status is SpecialistAnalysisStatus.COMPLETED
        and item.stance in {SpecialistStance.BULLISH, SpecialistStance.BEARISH}
    }
    if directional == {SpecialistStance.BULLISH}:
        return ThesisAnalyticalBias.BULLISH
    if directional == {SpecialistStance.BEARISH}:
        return ThesisAnalyticalBias.BEARISH
    return ThesisAnalyticalBias.INSUFFICIENT_DATA


def _limitations(
    consensus: SpecialistConsensus,
    analyses: tuple[SpecialistAnalysis, ...],
    bias: ThesisAnalyticalBias,
) -> tuple[str, ...]:
    values = {
        limitation
        for analysis in analyses
        for limitation in analysis.limitations
        if limitation.strip()
    }
    if consensus is SpecialistConsensus.CONFLICTED:
        values.add("Master synthesis contains conflicting analytical specialist artifacts.")
    if consensus is SpecialistConsensus.INSUFFICIENT_DATA or bias is ThesisAnalyticalBias.INSUFFICIENT_DATA:
        values.add("Master synthesis lacks sufficient directional analytical agreement.")
    values.add("This thesis is analytical only and has no trade, approval, or execution authority.")
    return tuple(sorted(values))
