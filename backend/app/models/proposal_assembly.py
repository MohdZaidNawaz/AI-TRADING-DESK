"""Immutable, internal-only input contracts for explicit proposal assembly."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.proposals import (
    PositionSizingIntent,
    ProposedTradeParameters,
    RiskReward,
    TradeDirection,
)
from app.models.schemas import APIModel
from app.models.trade_thesis import MasterTradeThesisReference


class ProposalAssemblyModel(APIModel):
    """Frozen provider-neutral contracts used before Sentinel evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExplicitTradeProposalInput(ProposalAssemblyModel):
    """Parameters explicitly supplied by a human or future external capability.

    This input is intentionally internal-only.  It carries no provider payloads,
    credentials, execution instructions, or approval authority.
    """

    input_id: str = Field(min_length=1, max_length=120)
    master_thesis_reference: MasterTradeThesisReference
    direction: TradeDirection
    reference_price: FiniteFloat
    stop_loss: FiniteFloat
    targets: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=25)
    risk_reward: RiskReward
    position_sizing_intent: PositionSizingIntent
    requested_risk_fraction: FiniteFloat
    rationale: str = Field(min_length=1, max_length=4_000)
    submitted_at: datetime
    expires_at: datetime | None = None

    @field_validator("input_id", "rationale")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("explicit proposal text cannot be blank")
        return normalized

    @field_validator("submitted_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("explicit proposal timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_explicit_values(self) -> "ExplicitTradeProposalInput":
        if self.position_sizing_intent is PositionSizingIntent.UNSPECIFIED:
            raise ValueError("position_sizing_intent must be explicitly supplied")
        if self.expires_at is not None and self.expires_at <= self.submitted_at:
            raise ValueError("explicit proposal expiry must be after submission")
        return self

    def as_parameters(self) -> ProposedTradeParameters:
        """Copy the supplied values without calculating or inferring any of them."""
        return ProposedTradeParameters(
            direction=self.direction,
            reference_price=self.reference_price,
            stop_loss=self.stop_loss,
            targets=self.targets,
            risk_reward=self.risk_reward,
            position_sizing_intent=self.position_sizing_intent,
            requested_risk_fraction=self.requested_risk_fraction,
        )
