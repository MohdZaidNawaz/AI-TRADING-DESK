"""Read-only, provider-bound execution evidence contracts.

These contracts establish an application-level validation boundary. They do
not authenticate hostile in-process code and never authorize execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.mt5 import BrokerInstrumentContract, MT5AccountSnapshot, MT5OrderStatusSnapshot, MT5PositionSnapshot
from app.models.portfolio_risk import SourceAccountKey
from app.models.schemas import APIModel


class ExecutionEvidenceAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class TrustedExecutionEvidenceModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionEvidenceRequest(TrustedExecutionEvidenceModel):
    """Exact operation scope for a read-only provider observation request."""

    account: SourceAccountKey
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    max_age_seconds: int = Field(gt=0, le=3600)
    expected_market_unit: str | None = Field(default=None, min_length=1, max_length=32)
    required_order_type: str | None = Field(default=None, min_length=1, max_length=40)
    required_filling_mode: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator(
        "canonical_symbol",
        "broker_symbol",
        "expected_market_unit",
        "required_order_type",
        "required_filling_mode",
        mode="before",
    )
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class MarketReferenceObservation(TrustedExecutionEvidenceModel):
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    bid: Decimal | None = None
    ask: Decimal | None = None
    unit: str = Field(min_length=1, max_length=32)
    observed_at: datetime

    @field_validator("canonical_symbol", "broker_symbol", "unit", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("bid", "ask", mode="before")
    @classmethod
    def finite_decimal(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise ValueError("market reference prices must be finite decimals") from error
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("market reference prices must be positive finite decimals")
        return parsed

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market reference timestamp requires timezone awareness")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_quote(self) -> "MarketReferenceObservation":
        if self.bid is None and self.ask is None:
            raise ValueError("market reference requires bid or ask")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("market reference bid cannot exceed ask")
        return self


class PortfolioRiskInputObservation(TrustedExecutionEvidenceModel):
    """Validated-input lineage for a later policy-owned risk calculation.

    It deliberately has no boolean safety outcome or trade recommendation.
    """

    account: SourceAccountKey
    position_snapshot_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1, max_length=160)
    policy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio risk input timestamp requires timezone awareness")
        return value.astimezone(UTC)


class ProviderExecutionEvidence(TrustedExecutionEvidenceModel):
    """Source response before application validation; unavailable has no facts."""

    availability: ExecutionEvidenceAvailability
    provider_id: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    reason_codes: tuple[str, ...] = ()
    instrument: BrokerInstrumentContract | None = None
    market: MarketReferenceObservation | None = None
    account: MT5AccountSnapshot | None = None
    positions: tuple[MT5PositionSnapshot, ...] = ()
    pending_orders: tuple[MT5OrderStatusSnapshot, ...] = ()
    portfolio_risk_inputs: PortfolioRiskInputObservation | None = None

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider evidence timestamp requires timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def unavailable_has_no_facts(self) -> "ProviderExecutionEvidence":
        facts = (self.instrument, self.market, self.account, self.positions, self.pending_orders, self.portfolio_risk_inputs)
        if self.availability is ExecutionEvidenceAvailability.UNAVAILABLE and any(facts):
            raise ValueError("unavailable evidence cannot contain fabricated observations")
        if self.availability is ExecutionEvidenceAvailability.UNAVAILABLE and not self.reason_codes:
            raise ValueError("unavailable evidence requires a reason code")
        return self


class ValidatedExecutionEvidence(TrustedExecutionEvidenceModel):
    """Validated read-only evidence; ``ready`` never grants execution authority."""

    request: ExecutionEvidenceRequest
    provider_id: str
    evaluated_at: datetime
    source_observed_at: datetime | None = None
    instrument: BrokerInstrumentContract | None = None
    market: MarketReferenceObservation | None = None
    account: MT5AccountSnapshot | None = None
    positions: tuple[MT5PositionSnapshot, ...] = ()
    pending_orders: tuple[MT5OrderStatusSnapshot, ...] = ()
    portfolio_risk_inputs: PortfolioRiskInputObservation | None = None
    reason_codes: tuple[str, ...] = ()
    ready: bool = False
    execution_authority: bool = False

    @field_validator("evaluated_at", "source_observed_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("validated evidence timestamp requires timezone awareness")
        return value.astimezone(UTC)

    @field_validator("reason_codes")
    @classmethod
    def stable_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def enforce_non_authorizing_result(self) -> "ValidatedExecutionEvidence":
        if self.execution_authority is not False:
            raise ValueError("execution evidence cannot grant execution authority")
        if self.ready and self.reason_codes:
            raise ValueError("ready evidence cannot contain failure codes")
        if self.ready and any(
            value is None
            for value in (
                self.source_observed_at,
                self.instrument,
                self.market,
                self.account,
                self.portfolio_risk_inputs,
            )
        ):
            raise ValueError("ready evidence requires complete validated observations")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
