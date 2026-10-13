"""Frozen, test-only contracts for the Phase 13 durable reservation boundary.

The records bind normalized safety facts and their source fingerprints. They
are not broker requests, retain no raw provider data, and cannot authorize a
submission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.proposals import TradeDirection
from app.models.schemas import APIModel


class DurableReservationStatus(str, Enum):
    RESERVED = "reserved"
    READY_FOR_ATTEMPT = "ready_for_attempt"
    ATTEMPT_STARTED = "attempt_started"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    REJECTED_TERMINAL = "rejected_terminal"
    COMPLETED_TERMINAL = "completed_terminal"
    REVOKED = "revoked"
    EXPIRED = "expired"


class DurableAuthorizationConsumptionState(str, Enum):
    UNCONSUMED = "unconsumed"
    CONSUMED_CONFIRMED = "consumed_confirmed"
    AMBIGUOUS = "ambiguous"


class DurableVerificationCheck(str, Enum):
    """Every safety fact that a Phase 13 verifier must explicitly attest."""

    INSTRUMENT = "instrument"
    MARKET_FRESHNESS = "market_freshness"
    PORTFOLIO_RISK = "portfolio_risk"
    SENTINEL_APPROVAL = "sentinel_approval"
    DEMO_AUTHORIZATION = "demo_authorization"
    ACCOUNT_BINDING = "account_binding"
    LINEAGE = "lineage"
    EXPIRY = "expiry"


class DurableExecutionModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DurableTestOnlyReservationEvaluation(DurableExecutionModel):
    """A structural, test-only evaluation of one immutable reservation.

    This is deliberately not an authorization or an authority assertion.
    Python callers can construct test data, so no value of this type can prove
    the current state of an approval, authorization, broker instrument, market,
    or portfolio source.  A future production boundary needs independently
    trusted source integrations before it may introduce an authoritative proof.
    """

    reservation_id: str = Field(min_length=1, max_length=180)
    reservation_fingerprint: str = Field(min_length=64, max_length=64)
    safety_evidence_fingerprint: str = Field(min_length=64, max_length=64)
    verified_at: datetime
    outcome: Literal["test_only_validated"]
    checks: tuple[DurableVerificationCheck, ...]
    test_only: Literal[True] = True

    @field_validator("verified_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("checks")
    @classmethod
    def complete_deterministic_checks(
        cls, value: tuple[DurableVerificationCheck, ...]
    ) -> tuple[DurableVerificationCheck, ...]:
        required = set(DurableVerificationCheck)
        if not value or set(value) != required or len(value) != len(required):
            raise ValueError("durable verification result requires every unique safety check")
        return tuple(sorted(value, key=lambda item: item.value))


class DurableBrokerInstrumentEvidence(DurableExecutionModel):
    """Sanitized source-scoped broker constraints for an explicit symbol."""

    source_id: str = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    minimum_volume: Decimal
    maximum_volume: Decimal
    volume_step: Decimal
    price_increment: Decimal
    observed_at: datetime
    expires_at: datetime

    @field_validator("canonical_symbol", "broker_symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("minimum_volume", "maximum_volume", "volume_step", "price_increment", mode="before")
    @classmethod
    def finite_decimal(cls, value: object) -> Decimal:
        return _decimal(value, "instrument numeric value")

    @model_validator(mode="after")
    def validate_instrument(self) -> "DurableBrokerInstrumentEvidence":
        if self.minimum_volume <= 0 or self.maximum_volume < self.minimum_volume or self.volume_step <= 0 or self.price_increment <= 0:
            raise ValueError("durable broker instrument constraints are invalid")
        if self.observed_at >= self.expires_at:
            raise ValueError("durable broker instrument evidence expiry must follow observation")
        return self


class DurableMarketReferenceEvidence(DurableExecutionModel):
    """A source-scoped observed price and explicit freshness policy."""

    source_id: str = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    reference_price: Decimal
    observed_at: datetime
    max_age_seconds: int = Field(gt=0, le=86_400)

    @field_validator("canonical_symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("reference_price", mode="before")
    @classmethod
    def finite_price(cls, value: object) -> Decimal:
        result = _decimal(value, "market reference price")
        if result <= 0:
            raise ValueError("market reference price must be positive")
        return result

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)


class DurablePortfolioRiskEvidence(DurableExecutionModel):
    """A validated source snapshot reference; this layer recalculates no risk."""

    source_id: str = Field(min_length=1, max_length=80)
    snapshot_id: str = Field(min_length=1, max_length=160)
    snapshot_fingerprint: str = Field(min_length=64, max_length=64)
    account_id: str = Field(min_length=1, max_length=160)
    account_reference: str = Field(min_length=1, max_length=160)
    broker_server: str = Field(min_length=1, max_length=160)
    risk_policy_fingerprint: str = Field(min_length=64, max_length=64)
    max_open_positions: int = Field(ge=0)
    current_open_positions: int = Field(ge=0)
    max_total_open_volume: Decimal
    current_open_volume: Decimal
    max_order_volume: Decimal
    status: Literal["clear"]
    observed_at: datetime
    expires_at: datetime

    @field_validator("observed_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("max_total_open_volume", "current_open_volume", "max_order_volume", mode="before")
    @classmethod
    def finite_volume(cls, value: object) -> Decimal:
        return _decimal(value, "portfolio risk volume")

    @model_validator(mode="after")
    def valid_window(self) -> "DurablePortfolioRiskEvidence":
        if self.observed_at >= self.expires_at:
            raise ValueError("portfolio risk evidence expiry must follow observation")
        if self.max_total_open_volume < 0 or self.current_open_volume < 0 or self.max_order_volume <= 0:
            raise ValueError("portfolio risk limits are invalid")
        if self.current_open_positions > self.max_open_positions or self.current_open_volume > self.max_total_open_volume:
            raise ValueError("portfolio risk snapshot exceeds its own limits")
        return self


class DurableSentinelApprovalEvidence(DurableExecutionModel):
    """Immutable approved Sentinel lineage, not a fresh approval authority."""

    decision_id: str = Field(min_length=1, max_length=160)
    approval_id: str = Field(min_length=1, max_length=160)
    approval_fingerprint: str = Field(min_length=64, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    analysis_id: str = Field(min_length=1, max_length=120)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    assessment_id: str = Field(min_length=1, max_length=160)
    assessment_fingerprint: str = Field(min_length=64, max_length=64)
    account_id: str = Field(min_length=1, max_length=160)
    status: Literal["approved"]
    evaluated_at: datetime
    expires_at: datetime

    @field_validator("evaluated_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def valid_window(self) -> "DurableSentinelApprovalEvidence":
        if self.evaluated_at >= self.expires_at:
            raise ValueError("Sentinel approval expiry must follow evaluation")
        return self


class DurableDemoAuthorizationEvidence(DurableExecutionModel):
    """Immutable authorized-demo lineage; its real store remains separate."""

    authorization_id: str = Field(min_length=1, max_length=160)
    authorization_fingerprint: str = Field(min_length=64, max_length=64)
    policy_id: str = Field(min_length=1, max_length=160)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    account_id: str = Field(min_length=1, max_length=160)
    account_reference: str = Field(min_length=1, max_length=160)
    broker_server: str = Field(min_length=1, max_length=160)
    proposal_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    analysis_id: str = Field(min_length=1, max_length=120)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    status: Literal["authorized"]
    issued_at: datetime
    expires_at: datetime

    @field_validator("canonical_symbol", "broker_symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def valid_window(self) -> "DurableDemoAuthorizationEvidence":
        if self.issued_at >= self.expires_at:
            raise ValueError("demo authorization expiry must follow issuance")
        return self


class DurableSafetyEvidence(DurableExecutionModel):
    """Frozen binding of source-scoped prerequisites for one reservation."""

    broker_instrument: DurableBrokerInstrumentEvidence
    market_reference: DurableMarketReferenceEvidence
    portfolio_risk: DurablePortfolioRiskEvidence
    sentinel_approval: DurableSentinelApprovalEvidence
    demo_authorization: DurableDemoAuthorizationEvidence

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class DurableExecutionReservation(DurableExecutionModel):
    """Exact immutable first-demo reservation lineage, without broker payloads.

    A store must additionally obtain an explicit test-only evaluator at
    construction.  Its structural output is not a trusted permission to
    progress outside this isolated, non-submitting Phase 13 foundation.
    """

    schema_version: Literal["2"] = "2"
    policy_version: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=160)
    account_reference: str = Field(min_length=1, max_length=160)
    broker_server: str = Field(min_length=1, max_length=160)
    proposal_id: str = Field(min_length=1, max_length=120)
    analysis_id: str = Field(min_length=1, max_length=120)
    proposal_fingerprint: str = Field(min_length=64, max_length=64)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    sentinel_approval_id: str = Field(min_length=1, max_length=160)
    sentinel_approval_fingerprint: str = Field(min_length=64, max_length=64)
    sentinel_policy_id: str = Field(min_length=1, max_length=160)
    controlled_demo_authorization_id: str = Field(min_length=1, max_length=160)
    controlled_demo_authorization_fingerprint: str = Field(min_length=64, max_length=64)
    execution_intent_id: str = Field(min_length=1, max_length=160)
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64)
    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    direction: TradeDirection
    quantity: Decimal
    order_type: str = Field(min_length=1, max_length=32)
    filling_mode: str = Field(min_length=1, max_length=32)
    stop_loss: Decimal
    take_profit: Decimal
    safety_evidence: DurableSafetyEvidence
    created_at: datetime
    expires_at: datetime
    audit_correlation_id: str = Field(min_length=1, max_length=160)
    status: DurableReservationStatus = DurableReservationStatus.RESERVED
    authorization_consumption: DurableAuthorizationConsumptionState = DurableAuthorizationConsumptionState.UNCONSUMED
    lifecycle_version: int = Field(default=1, ge=1)
    test_only: Literal[True] = True

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("canonical_symbol", "broker_symbol", "order_type", "filling_mode", mode="before")
    @classmethod
    def normalize_execution_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("quantity", "stop_loss", "take_profit", mode="before")
    @classmethod
    def finite_decimal(cls, value: object) -> Decimal:
        return _decimal(value, "durable reservation numeric value")

    @model_validator(mode="after")
    def validate_narrow_scope(self) -> "DurableExecutionReservation":
        if self.canonical_symbol != "XAUUSD" or self.broker_symbol != "XAUUSD":
            raise ValueError("durable reservation permits only explicit XAUUSD -> XAUUSD")
        if self.order_type != "MARKET" or self.filling_mode not in {"FOK", "IOC"}:
            raise ValueError("durable reservation permits only MARKET FOK/IOC")
        if self.quantity != Decimal("0.01"):
            raise ValueError("durable reservation quantity must be exactly 0.01")
        if self.created_at >= self.expires_at:
            raise ValueError("durable reservation expiry must follow creation")
        if self.stop_loss <= 0 or self.take_profit <= 0:
            raise ValueError("durable reservation requires explicit positive stop and target")
        market = self.safety_evidence.market_reference
        if market.canonical_symbol != self.canonical_symbol:
            raise ValueError("durable market reference symbol mismatch")
        if market.observed_at > self.created_at or (self.created_at - market.observed_at).total_seconds() > market.max_age_seconds:
            raise ValueError("durable market reference is stale or from the future")
        if self.direction is TradeDirection.LONG and not (self.stop_loss < market.reference_price < self.take_profit):
            raise ValueError("long reservation requires stop below and target above reference")
        if self.direction is TradeDirection.SHORT and not (self.take_profit < market.reference_price < self.stop_loss):
            raise ValueError("short reservation requires target below and stop above reference")
        instrument = self.safety_evidence.broker_instrument
        if (instrument.canonical_symbol, instrument.broker_symbol) != (self.canonical_symbol, self.broker_symbol):
            raise ValueError("durable broker instrument binding mismatch")
        if not instrument.minimum_volume <= self.quantity <= instrument.maximum_volume or (self.quantity - instrument.minimum_volume) % instrument.volume_step != 0:
            raise ValueError("durable broker volume constraints reject quantity")
        if any(self.created_at >= item.expires_at for item in (instrument, self.safety_evidence.portfolio_risk, self.safety_evidence.sentinel_approval, self.safety_evidence.demo_authorization)):
            raise ValueError("durable safety evidence is expired")
        risk = self.safety_evidence.portfolio_risk
        if (risk.account_id, risk.account_reference, risk.broker_server) != (self.account_id, self.account_reference, self.broker_server):
            raise ValueError("durable portfolio risk account binding mismatch")
        if risk.current_open_positions >= risk.max_open_positions or self.quantity > risk.max_order_volume or risk.current_open_volume + self.quantity > risk.max_total_open_volume:
            raise ValueError("durable portfolio risk limits reject first order")
        approval = self.safety_evidence.sentinel_approval
        if (approval.approval_id, approval.approval_fingerprint, approval.policy_id, approval.policy_version, approval.proposal_id, approval.proposal_fingerprint, approval.analysis_id, approval.evidence_fingerprint, approval.account_id) != (self.sentinel_approval_id, self.sentinel_approval_fingerprint, self.sentinel_policy_id, self.policy_version, self.proposal_id, self.proposal_fingerprint, self.analysis_id, self.evidence_fingerprint, self.account_id):
            raise ValueError("durable Sentinel lineage mismatch")
        authorization = self.safety_evidence.demo_authorization
        if (authorization.authorization_id, authorization.authorization_fingerprint, authorization.policy_id, authorization.proposal_id, authorization.proposal_fingerprint, authorization.analysis_id, authorization.evidence_fingerprint, authorization.execution_intent_id, authorization.execution_intent_fingerprint, authorization.account_id, authorization.account_reference, authorization.broker_server, authorization.canonical_symbol, authorization.broker_symbol) != (self.controlled_demo_authorization_id, self.controlled_demo_authorization_fingerprint, self.sentinel_policy_id, self.proposal_id, self.proposal_fingerprint, self.analysis_id, self.evidence_fingerprint, self.execution_intent_id, self.execution_intent_fingerprint, self.account_id, self.account_reference, self.broker_server, self.canonical_symbol, self.broker_symbol):
            raise ValueError("durable demo authorization lineage mismatch")
        if self.expires_at > min(approval.expires_at, authorization.expires_at, risk.expires_at, instrument.expires_at):
            raise ValueError("durable reservation outlives bound safety evidence")
        return self

    @computed_field(return_type=str)
    @property
    def execution_identity(self) -> str:
        return "durable-demo-execution-" + _fingerprint(self._execution_identity())

    @computed_field(return_type=str)
    @property
    def reservation_id(self) -> str:
        return "durable-reservation-" + _fingerprint(self._identity())

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint({**self._identity(), "status": self.status.value, "authorization_consumption": self.authorization_consumption.value, "lifecycle_version": self.lifecycle_version})

    def _execution_identity(self) -> dict[str, object]:
        return {"account_id": self.account_id, "account_reference": self.account_reference, "broker_server": self.broker_server, "execution_intent_id": self.execution_intent_id, "execution_intent_fingerprint": self.execution_intent_fingerprint, "canonical_symbol": self.canonical_symbol, "broker_symbol": self.broker_symbol, "quantity": str(self.quantity), "order_type": self.order_type, "filling_mode": self.filling_mode, "stop_loss": str(self.stop_loss), "take_profit": str(self.take_profit)}

    def _identity(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "policy_version": self.policy_version, **self._execution_identity(), "proposal_id": self.proposal_id, "analysis_id": self.analysis_id, "proposal_fingerprint": self.proposal_fingerprint, "evidence_fingerprint": self.evidence_fingerprint, "sentinel_approval_id": self.sentinel_approval_id, "sentinel_approval_fingerprint": self.sentinel_approval_fingerprint, "sentinel_policy_id": self.sentinel_policy_id, "controlled_demo_authorization_id": self.controlled_demo_authorization_id, "controlled_demo_authorization_fingerprint": self.controlled_demo_authorization_fingerprint, "direction": self.direction.value, "safety_evidence_fingerprint": self.safety_evidence.fingerprint, "created_at": self.created_at.isoformat(), "expires_at": self.expires_at.isoformat(), "audit_correlation_id": self.audit_correlation_id, "test_only": self.test_only}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("durable reservation timestamps require timezone awareness")
    return value.astimezone(UTC)


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()).hexdigest()
