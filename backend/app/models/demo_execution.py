"""Immutable, non-authorizing policy contracts for controlled demo readiness."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, FiniteFloat, computed_field, field_validator, model_validator

from app.models.execution import ExecutionIntent
from app.models.execution_state import ExecutionRecord
from app.models.mt5 import BrokerInstrumentContract, MT5AccountSnapshot, MT5PositionSnapshot
from app.models.portfolio_risk import SourceAccountKey
from app.models.schemas import APIModel


class DemoExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    DEMO_CONTROLLED = "demo_controlled"
    LIVE = "live"


class DemoExecutionModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DemoInstrumentBinding(DemoExecutionModel):
    """The sole explicit initial demo instrument binding; aliases are prohibited."""

    canonical_symbol: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)

    @field_validator("canonical_symbol", "broker_symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def only_validated_xauusd_mapping(self) -> "DemoInstrumentBinding":
        if self.canonical_symbol != "XAUUSD" or self.broker_symbol != "XAUUSD":
            raise ValueError("controlled demo readiness permits only explicit XAUUSD -> XAUUSD")
        return self


class ControlledDemoExecutionPolicy(DemoExecutionModel):
    """Versioned limits for a future demo-only authorization stage.

    ``enabled`` deliberately defaults to false and this policy is not wired to
    any adapter submission path. Every numeric limit is required: callers may
    not obtain an unsafe implicit default.
    """

    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=80)
    mode: DemoExecutionMode = DemoExecutionMode.DEMO_CONTROLLED
    enabled: bool = False
    allowed_account: SourceAccountKey
    instrument: DemoInstrumentBinding
    risk_currency: str = Field(min_length=3, max_length=12)
    allowed_order_types: tuple[str, ...] = Field(min_length=1)
    allowed_filling_modes: tuple[str, ...] = Field(min_length=1)
    max_order_volume: FiniteFloat = Field(gt=0)
    max_total_open_volume: FiniteFloat = Field(gt=0)
    max_open_positions: int = Field(ge=0)
    max_executions_in_window: int = Field(ge=0)
    controlled_window_seconds: int = Field(gt=0)
    max_monetary_risk_per_execution: FiniteFloat = Field(gt=0)
    max_aggregate_monetary_risk: FiniteFloat = Field(gt=0)
    max_stop_distance: FiniteFloat = Field(gt=0)
    max_order_notional: FiniteFloat = Field(gt=0)
    max_market_age_seconds: int = Field(gt=0)

    @field_validator("policy_id", "policy_version", "risk_currency", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("allowed_order_types", "allowed_filling_modes", mode="before")
    @classmethod
    def normalize_modes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        return tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def validate_safe_scope(self) -> "ControlledDemoExecutionPolicy":
        if self.allowed_account.source_id != "mt5":
            raise ValueError("controlled demo policy requires the mt5 source account")
        if self.allowed_account.account_reference is None:
            raise ValueError("controlled demo policy requires an exact broker/server identity")
        if self.mode is not DemoExecutionMode.DEMO_CONTROLLED:
            raise ValueError("this policy only defines the demo_controlled readiness boundary")
        if self.enabled:
            raise ValueError("controlled demo execution remains disabled during Stage 3")
        if self.allowed_order_types != ("MARKET",):
            raise ValueError("controlled demo policy permits only the validated MARKET order type")
        if not set(self.allowed_filling_modes).issubset({"FOK", "IOC"}):
            raise ValueError("controlled demo policy permits only FOK or IOC filling modes")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"fingerprint"}))


class DemoReadinessContext(DemoExecutionModel):
    """One immutable, account-bound read-only observation for policy evaluation."""

    account: MT5AccountSnapshot
    account_id: str = Field(min_length=1, max_length=160)
    instrument: BrokerInstrumentContract
    positions: tuple[MT5PositionSnapshot, ...] = ()
    execution_history: tuple[ExecutionRecord, ...] = ()
    market_observed_at: datetime
    captured_at: datetime

    @field_validator("market_observed_at", "captured_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("demo readiness timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("positions")
    @classmethod
    def deterministic_positions(cls, value: tuple[MT5PositionSnapshot, ...]) -> tuple[MT5PositionSnapshot, ...]:
        tickets = [position.ticket for position in value]
        if len(tickets) != len(set(tickets)):
            raise ValueError("demo readiness positions require unique tickets")
        return tuple(sorted(value, key=lambda position: position.ticket))

    @field_validator("execution_history")
    @classmethod
    def deterministic_history(cls, value: tuple[ExecutionRecord, ...]) -> tuple[ExecutionRecord, ...]:
        ids = [record.execution_id for record in value]
        if len(ids) != len(set(ids)):
            raise ValueError("demo readiness history requires unique execution ids")
        return tuple(sorted(value, key=lambda record: record.execution_id))

    @model_validator(mode="after")
    def validate_account_binding(self) -> "DemoReadinessContext":
        if self.account_id != self.account.account.account_id:
            raise ValueError("demo readiness context must bind positions to the exact account snapshot")
        return self


class DemoReadinessAssessment(DemoExecutionModel):
    """Sanitized policy outcome; it never grants submission authority."""

    policy_id: str
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    execution_intent_id: str
    execution_intent_fingerprint: str = Field(min_length=64, max_length=64)
    evaluated_at: datetime
    reason_codes: tuple[str, ...] = ()
    demo_execution_enabled: bool = False
    submission_allowed: bool = False

    @field_validator("evaluated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("demo readiness timestamps require timezone awareness")
        return value.astimezone(UTC)


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
