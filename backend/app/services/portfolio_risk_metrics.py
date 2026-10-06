"""Deterministic, exact calculations over Phase 5B-9 portfolio evidence."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.evidence.models import PortfolioRiskEvidence
from app.models.portfolio_risk import RiskLimitKind
from app.models.portfolio_risk_metrics import (
    CALCULATION_VERSION,
    PortfolioRiskMetric,
    PortfolioRiskMetricInputKind,
    PortfolioRiskMetricInputReference,
    PortfolioRiskMetricStatus,
    PortfolioRiskMetricType,
    PortfolioRiskMetricUnit,
    PortfolioRiskMetricsSnapshot,
    metric_identity,
    metrics_snapshot_identity,
)


class PortfolioRiskMetricsService:
    """Calculate only approved risk facts; no policy, sizing, or I/O."""

    calculation_version = CALCULATION_VERSION

    def calculate(self, evidence: PortfolioRiskEvidence) -> PortfolioRiskMetricsSnapshot:
        if not isinstance(evidence, PortfolioRiskEvidence):
            raise TypeError("evidence must be a PortfolioRiskEvidence")

        account_state = evidence.account_state
        metrics: list[PortfolioRiskMetric] = []
        metrics.append(
            self._ratio_metric(
                evidence,
                metric_type=PortfolioRiskMetricType.EQUITY_UTILIZATION,
                numerator=account_state.equity,
                denominator=account_state.balance,
                numerator_kind=PortfolioRiskMetricInputKind.EQUITY,
                denominator_kind=PortfolioRiskMetricInputKind.BALANCE,
            )
        )
        margin_total = None
        if account_state.margin_used is not None and account_state.margin_available is not None:
            margin_total = account_state.margin_used.value + account_state.margin_available.value
        metrics.append(
            self._ratio_metric(
                evidence,
                metric_type=PortfolioRiskMetricType.MARGIN_UTILIZATION,
                numerator=account_state.margin_used,
                denominator_value=margin_total,
                denominator_currency=account_state.currency,
                numerator_kind=PortfolioRiskMetricInputKind.MARGIN_USED,
                denominator_kind=PortfolioRiskMetricInputKind.MARGIN_AVAILABLE,
                denominator_reason="margin_used_plus_margin_available",
            )
        )

        # Drawdown is defined only against an explicit source-reported reference
        # equity; FRIDAY never invents a peak or substitutes balance.
        reference_equity = account_state.reference_equity
        drawdown_numerator = (
            reference_equity.value - account_state.equity.value
            if reference_equity is not None and account_state.equity is not None
            else None
        )
        metrics.append(
            self._ratio_metric(
                evidence,
                metric_type=PortfolioRiskMetricType.ACCOUNT_DRAWDOWN,
                numerator_value=drawdown_numerator,
                numerator_currency=account_state.currency,
                denominator=reference_equity,
                numerator_kind=PortfolioRiskMetricInputKind.EQUITY,
                denominator_kind=PortfolioRiskMetricInputKind.REFERENCE_EQUITY,
                extra_inputs=(
                    self._input(evidence, PortfolioRiskMetricInputKind.REFERENCE_EQUITY),
                ) if reference_equity is not None else (),
            )
        )

        daily_limits = tuple(
            limit for limit in evidence.risk_limits if limit.kind is RiskLimitKind.DAILY_LOSS
        )
        # Multiple reported daily-loss limits have no provider-neutral priority
        # rule, so they are insufficient rather than arbitrarily selecting one.
        daily_limit = daily_limits[0] if len(daily_limits) == 1 else None
        metrics.append(
            self._ratio_metric(
                evidence,
                metric_type=PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION,
                numerator=account_state.daily_loss,
                denominator=(daily_limit.value if daily_limit is not None else None),
                numerator_kind=PortfolioRiskMetricInputKind.DAILY_LOSS,
                denominator_kind=PortfolioRiskMetricInputKind.DAILY_LOSS_LIMIT,
                denominator_limit_id=(daily_limit.source_limit_id if daily_limit is not None else None),
            )
        )

        exposures: list[PortfolioRiskMetric] = []
        for position in evidence.positions:
            exposures.append(self._position_exposure_metric(evidence, position.source_position_id))
        metrics.extend(exposures)

        # Total exposure is only complete when every source-reported position
        # supplied quantity and reference price. Partial totals would fabricate
        # a portfolio concentration denominator.
        available_exposures = [metric for metric in exposures if metric.status is PortfolioRiskMetricStatus.AVAILABLE]
        complete_exposure_set = len(available_exposures) == len(exposures) and bool(exposures)
        total_exposure = sum((metric.value for metric in available_exposures if metric.value is not None), Decimal("0"))
        exposure_currency = evidence.account_state.currency
        for metric in exposures:
            metrics.append(
                self._concentration_metric(
                    evidence,
                    exposure=metric,
                    total_exposure=total_exposure if complete_exposure_set else None,
                    currency=exposure_currency,
                    all_exposures=tuple(exposures),
                )
            )

        return PortfolioRiskMetricsSnapshot(
            metrics_snapshot_id=metrics_snapshot_identity(
                account=evidence.account,
                source_snapshot_id=evidence.snapshot_id,
                source_snapshot_fingerprint=evidence.fingerprint,
                calculation_version=self.calculation_version,
            ),
            account=evidence.account,
            source_snapshot_id=evidence.snapshot_id,
            source_snapshot_occurrence_id=evidence.snapshot_occurrence_id,
            source_snapshot_version=evidence.snapshot_version,
            source_snapshot_fingerprint=evidence.fingerprint,
            calculation_version=self.calculation_version,
            as_of=evidence.as_of,
            calculated_at=evidence.received_at,
            metrics=tuple(metrics),
        )

    def _ratio_metric(
        self,
        evidence: PortfolioRiskEvidence,
        *,
        metric_type: PortfolioRiskMetricType,
        numerator: object = None,
        denominator: object = None,
        numerator_value: Decimal | None = None,
        denominator_value: Decimal | None = None,
        numerator_currency: str | None = None,
        denominator_currency: str | None = None,
        numerator_kind: PortfolioRiskMetricInputKind,
        denominator_kind: PortfolioRiskMetricInputKind,
        denominator_limit_id: str | None = None,
        denominator_reason: str | None = None,
        extra_inputs: tuple[PortfolioRiskMetricInputReference, ...] = (),
    ) -> PortfolioRiskMetric:
        if numerator is not None:
            numerator_value = numerator.value
            numerator_currency = evidence.account_state.currency
        if denominator is not None:
            denominator_value = denominator.value
            denominator_currency = evidence.account_state.currency
        inputs = [self._input(evidence, numerator_kind)]
        inputs.append(self._input(evidence, denominator_kind, source_limit_id=denominator_limit_id))
        inputs.extend(extra_inputs)
        limitations: tuple[str, ...] = ()
        value: Decimal | None = None
        status = PortfolioRiskMetricStatus.INSUFFICIENT_INPUTS
        if numerator_value is None or denominator_value is None:
            limitations = ("Required source-reported inputs are unavailable.",)
        elif numerator_currency != denominator_currency:
            limitations = ("Required source-reported inputs use incompatible currencies.",)
        elif denominator_value <= Decimal("0"):
            limitations = ("Required denominator is not positive.",)
        elif metric_type is PortfolioRiskMetricType.DAILY_LOSS_UTILIZATION and numerator_value < Decimal("0"):
            limitations = ("Source-reported daily loss basis cannot be negative.",)
        else:
            try:
                value = numerator_value / denominator_value
                if not value.is_finite():
                    raise InvalidOperation
                status = PortfolioRiskMetricStatus.AVAILABLE
            except (ArithmeticError, InvalidOperation):
                value = None
                limitations = ("Required source-reported inputs could not produce a finite ratio.",)
        if denominator_reason is not None:
            limitations = tuple(sorted(set((*limitations, f"Denominator basis: {denominator_reason}."))))
        return self._metric(
            evidence,
            metric_type=metric_type,
            status=status,
            value=value,
            unit=PortfolioRiskMetricUnit.RATIO,
            input_references=tuple(inputs),
            limitations=limitations,
        )

    def _position_exposure_metric(
        self, evidence: PortfolioRiskEvidence, position_id: str
    ) -> PortfolioRiskMetric:
        position = next(item for item in evidence.positions if item.source_position_id == position_id)
        inputs = (
            self._input(evidence, PortfolioRiskMetricInputKind.POSITION_QUANTITY, source_position_id=position_id),
            self._input(evidence, PortfolioRiskMetricInputKind.POSITION_REFERENCE_PRICE, source_position_id=position_id),
        )
        value: Decimal | None = None
        status = PortfolioRiskMetricStatus.INSUFFICIENT_INPUTS
        limitations: tuple[str, ...] = ()
        if position.reference_price is None or position.price_currency is None:
            limitations = ("Required source-reported quantity or reference price is unavailable.",)
        else:
            try:
                value = abs(position.quantity.value * position.reference_price.value)
                if not value.is_finite():
                    raise InvalidOperation
                status = PortfolioRiskMetricStatus.AVAILABLE
            except (ArithmeticError, InvalidOperation):
                value = None
                limitations = ("Required source-reported inputs could not produce a finite exposure.",)
        return self._metric(
            evidence,
            metric_type=PortfolioRiskMetricType.POSITION_EXPOSURE,
            source_position_id=position_id,
            status=status,
            value=value,
            unit=PortfolioRiskMetricUnit.CURRENCY,
            currency=position.price_currency,
            input_references=inputs,
            limitations=limitations,
        )

    def _concentration_metric(
        self,
        evidence: PortfolioRiskEvidence,
        *,
        exposure: PortfolioRiskMetric,
        total_exposure: Decimal | None,
        currency: str | None,
        all_exposures: tuple[PortfolioRiskMetric, ...],
    ) -> PortfolioRiskMetric:
        inputs = tuple(
            self._input(
                evidence,
                PortfolioRiskMetricInputKind.POSITION_EXPOSURE,
                source_position_id=item.source_position_id,
            )
            for item in all_exposures
            if item.source_position_id is not None
        )
        status = PortfolioRiskMetricStatus.INSUFFICIENT_INPUTS
        value: Decimal | None = None
        limitations: tuple[str, ...] = ()
        if exposure.status is not PortfolioRiskMetricStatus.AVAILABLE or total_exposure is None:
            limitations = ("All relevant source-reported position exposures are required.",)
        elif total_exposure <= Decimal("0"):
            limitations = ("Total source-reported portfolio exposure is not positive.",)
        elif exposure.currency != currency:
            limitations = ("Position exposure currency is incompatible with the portfolio total.",)
        else:
            value = exposure.value / total_exposure if exposure.value is not None else None
            if value is not None and value.is_finite():
                status = PortfolioRiskMetricStatus.AVAILABLE
            else:
                value = None
                limitations = ("Source-reported position exposure could not produce a finite concentration.",)
        return self._metric(
            evidence,
            metric_type=PortfolioRiskMetricType.PORTFOLIO_CONCENTRATION,
            source_position_id=exposure.source_position_id,
            status=status,
            value=value,
            unit=PortfolioRiskMetricUnit.RATIO,
            input_references=inputs,
            limitations=limitations,
        )

    def _metric(
        self,
        evidence: PortfolioRiskEvidence,
        *,
        metric_type: PortfolioRiskMetricType,
        status: PortfolioRiskMetricStatus,
        value: Decimal | None,
        unit: PortfolioRiskMetricUnit,
        input_references: tuple[PortfolioRiskMetricInputReference, ...],
        limitations: tuple[str, ...],
        source_position_id: str | None = None,
        currency: str | None = None,
    ) -> PortfolioRiskMetric:
        return PortfolioRiskMetric(
            metric_id=metric_identity(
                metric_type=metric_type,
                account=evidence.account,
                source_snapshot_id=evidence.snapshot_id,
                source_snapshot_fingerprint=evidence.fingerprint,
                source_position_id=source_position_id,
                calculation_version=self.calculation_version,
            ),
            metric_type=metric_type,
            account=evidence.account,
            source_snapshot_id=evidence.snapshot_id,
            source_snapshot_fingerprint=evidence.fingerprint,
            source_position_id=source_position_id,
            status=status,
            value=value,
            unit=unit,
            currency=currency,
            calculation_version=self.calculation_version,
            as_of=evidence.as_of,
            calculated_at=evidence.received_at,
            input_references=input_references,
            limitations=limitations,
        )

    @staticmethod
    def _input(
        evidence: PortfolioRiskEvidence,
        kind: PortfolioRiskMetricInputKind,
        *,
        source_position_id: str | None = None,
        source_limit_id: str | None = None,
    ) -> PortfolioRiskMetricInputReference:
        return PortfolioRiskMetricInputReference(
            kind=kind,
            source_snapshot_id=evidence.snapshot_id,
            source_snapshot_fingerprint=evidence.fingerprint,
            source_position_id=source_position_id,
            source_limit_id=source_limit_id,
        )
