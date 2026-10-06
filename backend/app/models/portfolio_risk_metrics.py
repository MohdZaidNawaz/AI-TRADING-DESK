"""Immutable, provider-neutral derived portfolio-risk metric contracts.

The Phase 5B-10 models describe deterministic facts calculated from a single
source-reported Phase 5B-9 portfolio snapshot.  They contain neither policy
thresholds nor any trade or execution instruction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.portfolio_risk import SourceAccountKey
from app.models.schemas import APIModel


CALCULATION_VERSION = "portfolio-risk-v1"


class PortfolioRiskMetricModel(APIModel):
    """Frozen base for internal portfolio-risk metric contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioRiskMetricType(str, Enum):
    EQUITY_UTILIZATION = "equity_utilization"
    MARGIN_UTILIZATION = "margin_utilization"
    ACCOUNT_DRAWDOWN = "account_drawdown"
    DAILY_LOSS_UTILIZATION = "daily_loss_utilization"
    POSITION_EXPOSURE = "position_exposure"
    PORTFOLIO_CONCENTRATION = "portfolio_concentration"


class PortfolioRiskMetricStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT_INPUTS = "insufficient_inputs"


class PortfolioRiskMetricUnit(str, Enum):
    RATIO = "ratio"
    CURRENCY = "currency"


class PortfolioRiskMetricInputKind(str, Enum):
    EQUITY = "equity"
    BALANCE = "balance"
    REFERENCE_EQUITY = "reference_equity"
    MARGIN_USED = "margin_used"
    MARGIN_AVAILABLE = "margin_available"
    DAILY_LOSS = "daily_loss"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    POSITION_QUANTITY = "position_quantity"
    POSITION_REFERENCE_PRICE = "position_reference_price"
    POSITION_EXPOSURE = "position_exposure"


class PortfolioRiskMetricInputReference(PortfolioRiskMetricModel):
    """A source-snapshot field used by one deterministic calculation."""

    kind: PortfolioRiskMetricInputKind
    source_snapshot_id: str = Field(min_length=1, max_length=160)
    source_snapshot_fingerprint: str = Field(min_length=64, max_length=64)
    source_position_id: str | None = Field(default=None, max_length=160)
    source_limit_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_scope(self) -> "PortfolioRiskMetricInputReference":
        position_kinds = {
            PortfolioRiskMetricInputKind.POSITION_QUANTITY,
            PortfolioRiskMetricInputKind.POSITION_REFERENCE_PRICE,
            PortfolioRiskMetricInputKind.POSITION_EXPOSURE,
        }
        if self.kind in position_kinds and self.source_position_id is None:
            raise ValueError("position metric inputs require source_position_id")
        if self.kind not in position_kinds and self.source_position_id is not None:
            raise ValueError("non-position metric inputs cannot include source_position_id")
        if self.kind is not PortfolioRiskMetricInputKind.DAILY_LOSS_LIMIT and self.source_limit_id is not None:
            raise ValueError("only daily-loss-limit inputs can include source_limit_id")
        return self


class PortfolioRiskMetric(PortfolioRiskMetricModel):
    """One calculation result or explicit insufficient-inputs fact."""

    metric_id: str = Field(min_length=1, max_length=160)
    metric_type: PortfolioRiskMetricType
    account: SourceAccountKey
    source_snapshot_id: str = Field(min_length=1, max_length=160)
    source_snapshot_fingerprint: str = Field(min_length=64, max_length=64)
    source_position_id: str | None = Field(default=None, max_length=160)
    status: PortfolioRiskMetricStatus
    value: Decimal | None = None
    unit: PortfolioRiskMetricUnit
    currency: str | None = Field(default=None, max_length=12)
    calculation_version: str = Field(min_length=1, max_length=80)
    as_of: datetime
    calculated_at: datetime
    input_references: tuple[PortfolioRiskMetricInputReference, ...] = ()
    limitations: tuple[str, ...] = ()

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("portfolio-risk metric values must be finite")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None

    @field_validator("as_of", "calculated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio-risk metric timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator("input_references")
    @classmethod
    def canonicalize_inputs(
        cls, values: tuple[PortfolioRiskMetricInputReference, ...]
    ) -> tuple[PortfolioRiskMetricInputReference, ...]:
        return tuple(
            sorted(
                dict.fromkeys(values),
                key=lambda item: (
                    item.kind.value,
                    item.source_position_id or "",
                    item.source_limit_id or "",
                    item.source_snapshot_id,
                ),
            )
        )

    @field_validator("limitations")
    @classmethod
    def canonicalize_limitations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip() for value in values if value.strip()}))

    @model_validator(mode="after")
    def validate_contract(self) -> "PortfolioRiskMetric":
        if self.status is PortfolioRiskMetricStatus.AVAILABLE and self.value is None:
            raise ValueError("available portfolio-risk metrics require a value")
        if self.status is not PortfolioRiskMetricStatus.AVAILABLE and self.value is not None:
            raise ValueError("unavailable portfolio-risk metrics cannot contain a value")
        if (
            self.status is PortfolioRiskMetricStatus.AVAILABLE
            and self.unit is PortfolioRiskMetricUnit.CURRENCY
            and self.currency is None
        ):
            raise ValueError("currency metrics require an explicit currency")
        if self.unit is PortfolioRiskMetricUnit.RATIO and self.currency is not None:
            raise ValueError("ratio metrics cannot contain a currency")
        if self.metric_type in {
            PortfolioRiskMetricType.POSITION_EXPOSURE,
            PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION,
        } and self.source_position_id is None:
            raise ValueError("position metrics require source_position_id")
        if self.metric_type not in {
            PortfolioRiskMetricType.POSITION_EXPOSURE,
            PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION,
        } and self.source_position_id is not None:
            raise ValueError("account metrics cannot contain source_position_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "metric_id": self.metric_id,
                "metric_type": self.metric_type.value,
                "account_id": self.account.account_id,
                "source_snapshot_id": self.source_snapshot_id,
                "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
                "source_position_id": self.source_position_id,
                "status": self.status.value,
                "value": _decimal_text(self.value),
                "unit": self.unit.value,
                "currency": self.currency,
                "calculation_version": self.calculation_version,
                "as_of": self.as_of.isoformat(),
                "input_references": [item.model_dump(mode="json") for item in self.input_references],
                "limitations": list(self.limitations),
            }
        )


class PortfolioRiskMetricsSnapshot(PortfolioRiskMetricModel):
    """Immutable derived metric set generated from one portfolio source snapshot."""

    metrics_snapshot_id: str = Field(min_length=1, max_length=160)
    account: SourceAccountKey
    source_snapshot_id: str = Field(min_length=1, max_length=160)
    source_snapshot_occurrence_id: str = Field(min_length=1, max_length=160)
    source_snapshot_version: int = Field(ge=1)
    source_snapshot_fingerprint: str = Field(min_length=64, max_length=64)
    calculation_version: str = Field(min_length=1, max_length=80)
    as_of: datetime
    calculated_at: datetime
    metrics: tuple[PortfolioRiskMetric, ...] = ()

    @field_validator("as_of", "calculated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio-risk metric snapshot timestamps must include a timezone")
        return value.astimezone(UTC)

    @field_validator("metrics")
    @classmethod
    def canonicalize_metrics(
        cls, values: tuple[PortfolioRiskMetric, ...]
    ) -> tuple[PortfolioRiskMetric, ...]:
        by_id = {item.metric_id: item for item in values}
        if len(by_id) != len(values):
            raise ValueError("portfolio-risk metric IDs must be unique within a snapshot")
        return tuple(by_id[key] for key in sorted(by_id))

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PortfolioRiskMetricsSnapshot":
        if any(
            item.account != self.account
            or item.source_snapshot_id != self.source_snapshot_id
            or item.source_snapshot_fingerprint != self.source_snapshot_fingerprint
            or item.calculation_version != self.calculation_version
            for item in self.metrics
        ):
            raise ValueError("all metrics must belong to the same source portfolio snapshot")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "account_id": self.account.account_id,
                "source_snapshot_id": self.source_snapshot_id,
                "source_snapshot_occurrence_id": self.source_snapshot_occurrence_id,
                "source_snapshot_version": self.source_snapshot_version,
                "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
                "calculation_version": self.calculation_version,
                "as_of": self.as_of.isoformat(),
                "metrics": [
                    {"metric_id": item.metric_id, "fingerprint": item.fingerprint}
                    for item in self.metrics
                ],
            }
        )


def metric_identity(
    *,
    metric_type: PortfolioRiskMetricType,
    account: SourceAccountKey,
    source_snapshot_id: str,
    source_snapshot_fingerprint: str,
    source_position_id: str | None,
    calculation_version: str,
) -> str:
    return "portfolio-risk-metric-" + _fingerprint(
        {
            "metric_type": metric_type.value,
            "account_id": account.account_id,
            "source_snapshot_id": source_snapshot_id,
            "source_snapshot_fingerprint": source_snapshot_fingerprint,
            "source_position_id": source_position_id,
            "calculation_version": calculation_version,
        }
    )


def metrics_snapshot_identity(
    *,
    account: SourceAccountKey,
    source_snapshot_id: str,
    source_snapshot_fingerprint: str,
    calculation_version: str,
) -> str:
    return "portfolio-risk-metrics-snapshot-" + _fingerprint(
        {
            "account_id": account.account_id,
            "source_snapshot_id": source_snapshot_id,
            "source_snapshot_fingerprint": source_snapshot_fingerprint,
            "calculation_version": calculation_version,
        }
    )


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return "0" if normalized == "-0" else normalized


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
