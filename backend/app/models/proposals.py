"""Provider-neutral Phase 3 proposal and Sentinel risk-gate contracts.

These models deliberately describe research proposals and approvals only.  They
do not contain provider payloads, credentials, account adapters, or execution
instructions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, FiniteFloat, field_validator

from app.models.events import AssetClass, DataDomain
from app.models.risk_assessment import SentinelRiskAssessment
from app.models.schemas import APIModel, AnalyzeResponse
from app.models.specialist_analysis import SpecialistStance
from app.models.trade_thesis import MasterTradeThesisReference


class ProposalModel(APIModel):
    """Immutable internal contract used by the non-executable proposal flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TradeDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class PositionSizingIntent(str, Enum):
    """The requested sizing approach; no account calculation is performed here."""

    UNSPECIFIED = "unspecified"
    RISK_FRACTION = "risk_fraction"
    FIXED_UNITS = "fixed_units"


class TradeProposalStatus(str, Enum):
    DRAFT = "draft"
    PENDING_RISK = "pending_risk"
    APPROVED = "approved"
    REJECTED = "rejected"
    INSUFFICIENT_DATA = "insufficient_data"
    EXPIRED = "expired"


class ExecutionEligibility(str, Enum):
    """Phase 3 has no enabled execution state."""

    NOT_AUTHORIZED = "not_authorized"


class SentinelDecisionStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    INSUFFICIENT_DATA = "insufficient_data"


class EvidenceReference(ProposalModel):
    """A stable reference to safe, normalized evidence provenance."""

    event_id: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=80)
    domain: DataDomain
    timestamp: datetime
    symbol: str | None = Field(default=None, max_length=40)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp values must include a timezone")
        return value.astimezone(UTC)


class SpecialistAnalysisReference(ProposalModel):
    """A compact specialist-output reference, not a copy of provider data.

    The optional Phase 4 fields preserve traceability to the immutable internal
    artifact.  ``stance`` remains analytical context and is not a trade direction.
    """

    agent_id: str = Field(min_length=1, max_length=80)
    status: Literal["completed", "skipped", "error"]
    confidence: FiniteFloat
    summary_digest: str = Field(min_length=1, max_length=64)
    specialist_analysis_id: str | None = Field(default=None, min_length=1, max_length=120)
    specialist_analysis_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    stance: SpecialistStance | None = None


class ObservedMarketReference(ProposalModel):
    """An observed normalized price fact; it is not an executable entry instruction."""

    evidence: EvidenceReference
    price: FiniteFloat
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(UTC)


class RiskReward(ProposalModel):
    """Explicit proposed risk/reward values that Sentinel independently verifies."""

    risk_distance: FiniteFloat
    reward_distance: FiniteFloat
    ratio: FiniteFloat


class ProposedTradeParameters(ProposalModel):
    """Optional, non-executable proposed parameters supplied by a future capability."""

    direction: TradeDirection | None = None
    reference_price: FiniteFloat | None = None
    stop_loss: FiniteFloat | None = None
    targets: tuple[FiniteFloat, ...] = ()
    risk_reward: RiskReward | None = None
    position_sizing_intent: PositionSizingIntent = PositionSizingIntent.UNSPECIFIED
    requested_risk_fraction: FiniteFloat | None = None


class TradeProposal(ProposalModel):
    """A typed research proposal that remains non-executable in Phase 3."""

    proposal_id: str = Field(min_length=1, max_length=120)
    analysis_id: str = Field(min_length=1, max_length=120)
    evidence_fingerprint: str = Field(min_length=1, max_length=64)
    evidence_references: tuple[EvidenceReference, ...] = ()
    specialist_references: tuple[SpecialistAnalysisReference, ...] = ()
    master_thesis_reference: MasterTradeThesisReference | None = None
    symbol: str | None = Field(default=None, max_length=40)
    asset_class: AssetClass = AssetClass.UNKNOWN
    observed_market_reference: ObservedMarketReference | None = None
    parameters: ProposedTradeParameters | None = None
    thesis: str = Field(min_length=1, max_length=4_000)
    created_at: datetime
    expires_at: datetime | None = None
    status: TradeProposalStatus = TradeProposalStatus.DRAFT
    execution_eligibility: ExecutionEligibility = ExecutionEligibility.NOT_AUTHORIZED
    missing_requirements: tuple[str, ...] = ()

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("proposal timestamps must include a timezone")
        return value.astimezone(UTC)


class SpecialistSignal(ProposalModel):
    """A future structured specialist view used only when explicitly supplied."""

    agent_id: str = Field(min_length=1, max_length=80)
    direction: TradeDirection
    confidence: FiniteFloat


class PortfolioRiskContext(ProposalModel):
    """Explicit portfolio values supplied to Sentinel; no adapter populates them today."""

    account_equity: FiniteFloat
    current_exposure_fraction: FiniteFloat
    max_total_exposure_fraction: FiniteFloat


class RiskContext(ProposalModel):
    """Explicit policy and portfolio context required before Sentinel can approve."""

    as_of: datetime
    max_market_evidence_age_seconds: int | None = None
    approval_ttl_seconds: int | None = None
    portfolio: PortfolioRiskContext | None = None
    max_risk_fraction: FiniteFloat | None = None
    required_agreeing_specialists: int | None = None
    reject_conflicting_signals: bool | None = None
    specialist_signals: tuple[SpecialistSignal, ...] = ()
    news_risk_assessed: bool | None = None

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value.astimezone(UTC)


class SentinelDecision(ProposalModel):
    """Sentinel's standalone, non-executable approval or refusal record."""

    decision_id: str = Field(min_length=1, max_length=120)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=1, max_length=64)
    analysis_id: str = Field(min_length=1, max_length=120)
    decision: SentinelDecisionStatus
    reasons: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    evidence_fingerprint: str = Field(min_length=1, max_length=64)
    evaluated_at: datetime
    expires_at: datetime | None = None
    policy_version: str = Field(min_length=1, max_length=80)
    policy_id: str = Field(min_length=1, max_length=120)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    assessment_id: str = Field(min_length=1, max_length=160)
    assessment_fingerprint: str = Field(min_length=64, max_length=64)
    approval_id: str | None = Field(default=None, max_length=120)

    @field_validator("evaluated_at", "expires_at")
    @classmethod
    def normalize_decision_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision timestamps must include a timezone")
        return value.astimezone(UTC)


class VerifiedSentinelApproval(ProposalModel):
    """Internal proof returned only by the lock-protected approval registry."""

    proposal_id: str
    proposal_fingerprint: str
    analysis_id: str
    evidence_fingerprint: str
    decision_id: str
    approval_id: str
    expires_at: datetime


class DisabledPilotResult(ProposalModel):
    """The only Phase 3 Pilot result after a verified approval is consumed."""

    status: Literal["DISABLED_NO_EXECUTION"] = "DISABLED_NO_EXECUTION"
    proposal_id: str
    approval_id: str
    message: str


class TradeProposalWorkflowResult(ProposalModel):
    """Internal result combining an unchanged analysis response with Phase 3 artifacts."""

    analysis: AnalyzeResponse
    proposal: TradeProposal
    sentinel_decision: SentinelDecision
    risk_assessment: SentinelRiskAssessment | None = None
