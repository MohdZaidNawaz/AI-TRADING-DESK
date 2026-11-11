"""Deterministic event-driven historical validation over cached market evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import math
from statistics import fmean, pstdev
from typing import Callable, Protocol
from functools import lru_cache

from app.agents.base import AnalysisContext
from app.agents.master import MasterAgent
from app.agents.specialists import OracleQuantAgent, SentinelRiskOfficerAgent
from app.evidence.models import (
    AgentEvidence,
    MarketBarEvidence,
    MarketSeriesEvidence,
    SeriesContinuity,
    SeriesGap,
)
from app.models.backtesting import (
    BacktestCostAssessment,
    BacktestDataQuality,
    BacktestDirection,
    BacktestExitReason,
    BacktestMetric,
    BacktestProvenance,
    BacktestReport,
    BacktestRequest,
    BacktestResponse,
    BacktestStatus,
    BacktestTrade,
    CostVerificationStatus,
    EquityPoint,
    MetricStatus,
    PerformanceMetrics,
    ReturnSeriesMetrics,
    ValidationWindow,
    ValidationWindowKind,
)
from app.models.market_series import MarketBarState, canonical_market_symbol, timeframe_seconds
from app.models.research_requests import InstrumentResolutionStatus
from app.models.risk_strategy_intelligence import (
    CalculationStatus,
    PositionSizingResult,
    RiskSizingContext,
    ScenarioDirection,
    ScenarioStatus,
)
from app.models.technical_analysis import TrendDirection
from app.services.liquidity_intelligence import LiquidityIntelligenceService
from app.services.research_requests import ResearchRequestService
from app.services.risk_strategy_intelligence import RiskStrategyIntelligenceService
from app.services.technical_analysis import TechnicalAnalysisService


class HistoricalEvidenceOwner(Protocol):
    def evidence_snapshot(self, *, symbol: str | None = None): ...


@dataclass(frozen=True, slots=True)
class BacktestSignal:
    """Internal deterministic setup produced only at a completed-candle close."""

    direction: BacktestDirection
    signal_at: datetime
    entry_lower: float
    entry_upper: float
    entry_reference: float
    stop_loss: float
    take_profit: float
    evidence_ids: tuple[str, ...] = ()
    position_sizing: PositionSizingResult | None = None


SignalEvaluator = Callable[
    [MarketSeriesEvidence, RiskSizingContext | None, datetime],
    BacktestSignal | None,
]


@dataclass(slots=True)
class _OpenPosition:
    signal: BacktestSignal
    entered_at: datetime
    entry_price: float
    entry_bar_id: str
    holding_bars: int = 0


class BacktestingService:
    """Validate one documented strategy against existing cached evidence only."""

    def __init__(
        self,
        *,
        evidence_owner: HistoricalEvidenceOwner,
        resolver: ResearchRequestService,
        signal_evaluator: SignalEvaluator | None = None,
        minimum_history_bars: int = 200,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if minimum_history_bars < 1 or minimum_history_bars > 500:
            raise ValueError("minimum_history_bars must be between 1 and 500")
        self._evidence_owner = evidence_owner
        self._resolver = resolver
        self._signal_evaluator = signal_evaluator or _ExistingStrategySignalEvaluator()
        self._minimum_history_bars = minimum_history_bars
        self._now = now or (lambda: datetime.now(UTC))
        self._oracle = OracleQuantAgent()
        self._sentinel = SentinelRiskOfficerAgent()
        self._master = MasterAgent()

    def run(self, request: BacktestRequest) -> BacktestResponse:
        resolution = self._resolver.resolve_instrument(request.symbol)
        canonical = resolution.canonical_symbol
        if resolution.status is not InstrumentResolutionStatus.RESOLVED or canonical is None:
            report = _unavailable_report(
                request,
                evaluated_at=self._now(),
                warning="INSTRUMENT_IDENTITY_NOT_RESOLVED",
                limitation="The requested instrument was not resolved by the existing catalogue/reference contract.",
            )
            return self._integrate(request.symbol, report, None)

        storage_symbol = canonical_market_symbol(canonical)
        snapshot = self._evidence_owner.evidence_snapshot(symbol=storage_symbol)
        candidates = tuple(
            item
            for item in snapshot.market_series
            if canonical_market_symbol(item.symbol) == storage_symbol
            and item.timeframe == request.timeframe
            and (request.source_id is None or item.provenance.source_id == request.source_id)
        )
        if not candidates:
            report = _unavailable_report(
                request.model_copy(update={"symbol": canonical}),
                evaluated_at=self._now(),
                warning="HISTORICAL_SERIES_UNAVAILABLE",
                limitation="No cached validated series matched the resolved instrument, timeframe, and source.",
            )
            return self._integrate(canonical, report, None)
        if len(candidates) > 1:
            report = _unavailable_report(
                request.model_copy(update={"symbol": canonical}),
                evaluated_at=self._now(),
                warning="HISTORICAL_SOURCE_AMBIGUOUS",
                limitation="Multiple cached sources matched; source_id is required to avoid silently mixing providers.",
            )
            return self._integrate(canonical, report, None)

        normalized_request = request.model_copy(update={"symbol": canonical})
        report = self.run_series(normalized_request, candidates[0])
        return self._integrate(canonical, report, candidates[0])

    def run_series(
        self,
        request: BacktestRequest,
        series: MarketSeriesEvidence,
    ) -> BacktestReport:
        evaluation = _utc(self._now())
        selected = tuple(
            bar
            for bar in series.bars
            if (request.start_at is None or bar.opened_at >= request.start_at)
            and (request.end_at is None or bar.closed_at <= request.end_at)
        )
        completed = tuple(bar for bar in selected if bar.state is MarketBarState.COMPLETED)
        provisional_count = len(selected) - len(completed)
        ordered = all(
            previous.opened_at < current.opened_at
            for previous, current in zip(selected, selected[1:], strict=False)
        )
        timestamps = [bar.opened_at for bar in selected]
        duplicate_count = len(timestamps) - len(set(timestamps))
        continuity = _continuity(completed, timeframe_seconds(series.timeframe))
        quality = BacktestDataQuality(
            validated_bar_count=len(selected),
            completed_bar_count=len(completed),
            provisional_bar_count=provisional_count,
            duplicate_timestamp_count=duplicate_count,
            missing_interval_count=continuity.missing_interval_count,
            is_ordered=ordered,
            coverage_start=completed[0].opened_at if completed else None,
            coverage_end=completed[-1].closed_at if completed else None,
        )
        provenance = BacktestProvenance(
            series_id=series.series_id,
            series_fingerprint=series.fingerprint,
            source_id=series.provenance.source_id,
            source_name=series.provenance.source_name,
            retrieved_at=series.provenance.latest_received_at,
            timeframe=series.timeframe,
            venue=series.venue,
        )
        warnings: list[str] = []
        if provisional_count:
            warnings.append("PROVISIONAL_CANDLES_EXCLUDED")
        if continuity.missing_interval_count:
            warnings.append("MISSING_INTERVALS_PRESENT")
        if duplicate_count:
            warnings.append("DUPLICATE_TIMESTAMPS_REJECTED")
        if not ordered:
            warnings.append("CANDLES_NOT_STRICTLY_ORDERED")
        if duplicate_count or not ordered:
            return _base_report(
                request,
                evaluation,
                BacktestStatus.UNAVAILABLE,
                quality,
                provenance,
                warnings,
                ("Historical ordering or identity validation failed; no simulation was run.",),
            )
        if len(completed) < self._minimum_history_bars + 1:
            warnings.append("INSUFFICIENT_COMPLETED_CANDLES")
            return _base_report(
                request,
                evaluation,
                BacktestStatus.INSUFFICIENT_DATA,
                quality,
                provenance,
                warnings,
                (
                    f"The documented strategy requires at least {self._minimum_history_bars + 1} completed candles.",
                ),
            )

        cost_assessment = _assess_costs(request, evaluation)
        if cost_assessment.status is CostVerificationStatus.INVALID:
            warnings.append("VERIFIED_COSTS_INVALID_AND_NOT_APPLIED")
        elif cost_assessment.status is CostVerificationStatus.INCOMPLETE:
            warnings.append("VERIFIED_TRADING_COSTS_INCOMPLETE_GROSS_RESULTS_ONLY")
        elif cost_assessment.status is CostVerificationStatus.UNAVAILABLE:
            warnings.append("VERIFIED_TRADING_COSTS_UNAVAILABLE_GROSS_RESULTS_ONLY")
        simulation_request = (
            request
            if cost_assessment.status is CostVerificationStatus.VERIFIED
            else request.model_copy(update={"verified_costs": None})
        )
        trades, simulation_warnings, incomplete_trade_count = self._simulate(
            simulation_request,
            series,
            completed,
            evaluated_at=evaluation,
        )
        warnings.extend(simulation_warnings)
        include_net = cost_assessment.status is CostVerificationStatus.VERIFIED
        if include_net and any(item.net_return_pct is None for item in trades):
            include_net = False
            cost_assessment = cost_assessment.model_copy(
                update={
                    "status": CostVerificationStatus.INCOMPLETE,
                    "missing_components": tuple(
                        dict.fromkeys((*cost_assessment.missing_components, "verified_position_size_for_commission"))
                    ),
                }
            )
            warnings.append("NET_RESULTS_UNAVAILABLE_FOR_ONE_OR_MORE_TRADES")
        metrics, curve, net_curve = _metrics(trades, include_net=include_net)
        windows = self._validation_windows(
            simulation_request,
            series,
            completed,
            evaluated_at=evaluation,
            include_net=include_net,
        )
        if len(trades) < 30:
            warnings.append("SMALL_TRADE_SAMPLE")
        if incomplete_trade_count:
            warnings.append("OPEN_POSITION_EXCLUDED_AT_SAMPLE_END")
        status = BacktestStatus.AVAILABLE
        if warnings or not trades:
            status = BacktestStatus.PARTIAL
        return BacktestReport(
            status=status,
            symbol=request.symbol,
            timeframe=series.timeframe,
            evaluated_at=evaluation,
            data_quality=quality,
            provenance=provenance,
            parameters=request.parameters,
            cost_basis="verified_net" if include_net else "gross_only",
            cost_assessment=cost_assessment,
            trades=trades,
            incomplete_trade_count=incomplete_trade_count,
            metrics=metrics,
            equity_curve=curve,
            net_equity_curve=net_curve,
            validation_windows=windows,
            warnings=tuple(dict.fromkeys(warnings)),
            limitations=(
                "Historical validation describes this bounded sample and is not a forecast or profitability guarantee.",
                "Signals use completed-candle prefixes only; entry is eligible on the next candle only.",
                "When stop and target occur in one candle, the stop is applied first because intrabar ordering is unavailable.",
                "Missing candles are reported and never filled; no broker execution or live provider request occurs.",
                "Development and out-of-sample periods are chronological; this engine performs no parameter tuning.",
            ),
        )

    def _simulate(
        self,
        request: BacktestRequest,
        series: MarketSeriesEvidence,
        bars: tuple[MarketBarEvidence, ...],
        *,
        evaluated_at: datetime,
        signal_start_index: int = 0,
        end_index: int | None = None,
    ) -> tuple[tuple[BacktestTrade, ...], tuple[str, ...], int]:
        end_index = len(bars) if end_index is None else end_index
        bounded_bars = bars[:end_index]
        pending: BacktestSignal | None = None
        position: _OpenPosition | None = None
        trades: list[BacktestTrade] = []
        warnings: list[str] = []
        for index, bar in enumerate(bounded_bars):
            if pending is not None and position is None:
                position = _try_entry(pending, bar)
                if position is None and _bar_gapped_beyond_stop(pending, bar):
                    warnings.append("ENTRY_SKIPPED_UNRESOLVED_GAP_SEQUENCE")
                pending = None

            if position is not None:
                position.holding_bars += 1
                exit_result = _exit_for_bar(
                    position,
                    bar,
                    maximum_holding=(
                        position.holding_bars >= request.parameters.maximum_holding_bars
                    ),
                )
                if exit_result is not None:
                    exit_price, reason = exit_result
                    trades.append(
                        _close_trade(
                            position,
                            bar,
                            exit_price,
                            reason,
                            request,
                            index=len(trades),
                        )
                    )
                    position = None

            if (
                position is None
                and pending is None
                and index >= self._minimum_history_bars - 1
                and index >= signal_start_index
                and index < len(bounded_bars) - 1
            ):
                prefix = _prefix_series(series, bars[: index + 1])
                pending = self._signal_evaluator(
                    prefix,
                    request.sizing_context,
                    evaluated_at,
                )

        incomplete = int(position is not None)
        if incomplete:
            warnings.append("OPEN_POSITION_EXCLUDED_AT_WINDOW_END")
        return tuple(trades), tuple(dict.fromkeys(warnings)), incomplete

    def _validation_windows(
        self,
        request: BacktestRequest,
        series: MarketSeriesEvidence,
        bars: tuple[MarketBarEvidence, ...],
        *,
        evaluated_at: datetime,
        include_net: bool,
    ) -> tuple[ValidationWindow, ...]:
        split = max(1, min(len(bars) - 1, int(len(bars) * request.parameters.development_fraction)))
        definitions = [
            ("development", ValidationWindowKind.DEVELOPMENT, 0, split),
            ("out-of-sample", ValidationWindowKind.OUT_OF_SAMPLE, split, len(bars)),
        ]
        window = request.parameters.walk_forward_window_bars
        step = request.parameters.walk_forward_step_bars
        if window is not None and step is not None:
            index = 0
            ordinal = 1
            while index + window <= len(bars):
                definitions.append((f"walk-forward-{ordinal}", ValidationWindowKind.WALK_FORWARD, index, index + window))
                index += step
                ordinal += 1
        results = []
        for name, kind, start, end in definitions:
            scoped, simulation_warnings, incomplete = self._simulate(
                request,
                series,
                bars,
                evaluated_at=evaluated_at,
                signal_start_index=start,
                end_index=end,
            )
            metrics, _, _ = _metrics(scoped, include_net=include_net)
            window_warnings = list(simulation_warnings)
            if end - start < 30:
                window_warnings.append("SMALL_OBSERVATION_WINDOW")
            results.append(
                ValidationWindow(
                    window_id=name,
                    kind=kind,
                    start_at=bars[start].opened_at,
                    end_at=bars[end - 1].closed_at,
                    observation_count=end - start,
                    incomplete_trade_count=incomplete,
                    metrics=metrics,
                    warnings=tuple(dict.fromkeys(window_warnings)),
                )
            )
        return tuple(results)

    def _integrate(
        self,
        symbol: str,
        report: BacktestReport,
        series: MarketSeriesEvidence | None,
    ) -> BacktestResponse:
        context = AnalysisContext(
            symbol=symbol,
            query=f"Historical validation for {symbol} on {report.timeframe}",
            evidence=AgentEvidence(market_series=(() if series is None else (series,))),
            backtest_validation=report,
        )
        quantitative = self._oracle.analyze(context)
        risk = self._sentinel.analyze(context)
        synthesis = self._master.synthesize(context, [quantitative, risk])
        return BacktestResponse(
            report=report,
            quantitative_specialist=quantitative,
            risk_specialist=risk,
            master_synthesis=synthesis,
        )


class _ExistingStrategySignalEvaluator:
    """Adapt existing Phase 5.3/5.5/5.6 outputs without adding a new strategy."""

    def __init__(self) -> None:
        self._technical = TechnicalAnalysisService()
        self._liquidity = LiquidityIntelligenceService()
        self._risk = RiskStrategyIntelligenceService()

    def __call__(
        self,
        series: MarketSeriesEvidence,
        sizing_context: RiskSizingContext | None,
        evaluated_at: datetime,
    ) -> BacktestSignal | None:
        technical = self._technical.analyze((series,), requested_timeframes=(series.timeframe,))
        analysis = technical.analyses[0]
        mapping = {
            TrendDirection.BULLISH: (ScenarioDirection.BULLISH, BacktestDirection.LONG),
            TrendDirection.BEARISH: (ScenarioDirection.BEARISH, BacktestDirection.SHORT),
        }
        direction = mapping.get(analysis.trend_direction)
        if direction is None:
            return None
        liquidity = self._liquidity.analyze(
            (series,),
            requested_timeframes=(series.timeframe,),
            evaluated_at=evaluated_at,
        )
        risk = self._risk.analyze(
            subject=series.symbol,
            technical=technical,
            liquidity=liquidity,
            macro=None,
            news=None,
            market_series=(series,),
            sizing_context=sizing_context,
            evaluated_at=evaluated_at,
        )
        scenario = next(item for item in risk.scenarios if item.direction is direction[0])
        if (
            scenario.status is not ScenarioStatus.CONDITIONAL
            or scenario.entry_zone is None
            or scenario.stop_loss_candidate is None
            or not scenario.target_candidates
        ):
            return None
        entry = scenario.entry_zone
        stop = float(scenario.stop_loss_candidate)
        target = float(scenario.target_candidates[0])
        if direction[1] is BacktestDirection.LONG and not stop < entry.reference_price < target:
            return None
        if direction[1] is BacktestDirection.SHORT and not target < entry.reference_price < stop:
            return None
        latest = series.latest_completed_bar
        if latest is None:
            return None
        return BacktestSignal(
            direction=direction[1],
            signal_at=latest.closed_at,
            entry_lower=float(entry.lower),
            entry_upper=float(entry.upper),
            entry_reference=float(entry.reference_price),
            stop_loss=stop,
            take_profit=target,
            evidence_ids=tuple(dict.fromkeys((*entry.evidence_ids, *scenario.supporting_evidence))),
            position_sizing=scenario.position_sizing,
        )


def _prefix_series(
    series: MarketSeriesEvidence,
    bars: tuple[MarketBarEvidence, ...],
) -> MarketSeriesEvidence:
    continuity = _continuity(bars, timeframe_seconds(series.timeframe))
    return replace(
        series,
        bars=bars,
        oldest_completed_at=bars[0].closed_at,
        latest_completed_at=bars[-1].closed_at,
        completed_count=len(bars),
        provisional_count=0,
        continuity=continuity,
    )


def _continuity(
    bars: tuple[MarketBarEvidence, ...],
    interval_seconds: int,
) -> SeriesContinuity:
    if not bars:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return SeriesContinuity(interval_seconds, epoch, epoch, True, True, 0, ())
    gaps: list[SeriesGap] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        delta = (current.opened_at - previous.opened_at).total_seconds()
        expected_open = previous.closed_at
        if current.opened_at != expected_open:
            gaps.append(
                SeriesGap(
                    expected_open_at=expected_open,
                    actual_open_at=current.opened_at,
                    missing_interval_count=max(0, math.ceil(delta / interval_seconds) - 1),
                )
            )
    missing = sum(item.missing_interval_count for item in gaps)
    ordered = all(left.opened_at < right.opened_at for left, right in zip(bars, bars[1:], strict=False))
    return SeriesContinuity(
        expected_interval_seconds=interval_seconds,
        first_opened_at=bars[0].opened_at,
        last_closed_at=bars[-1].closed_at,
        is_ordered=ordered,
        is_contiguous=ordered and not gaps,
        missing_interval_count=missing,
        gaps=tuple(gaps),
    )


def _try_entry(signal: BacktestSignal, bar: MarketBarEvidence) -> _OpenPosition | None:
    if signal.direction is BacktestDirection.LONG:
        if bar.open < signal.stop_loss and bar.high >= signal.entry_lower:
            return None
    elif bar.open > signal.stop_loss and bar.low <= signal.entry_upper:
        return None
    if signal.entry_lower <= bar.open <= signal.entry_upper:
        price = bar.open
    elif bar.low <= signal.entry_reference <= bar.high:
        price = signal.entry_reference
    elif bar.low <= signal.entry_upper and bar.high >= signal.entry_lower:
        price = min(signal.entry_upper, max(signal.entry_lower, signal.entry_reference))
    else:
        return None
    if signal.direction is BacktestDirection.LONG and not signal.stop_loss < price < signal.take_profit:
        return None
    if signal.direction is BacktestDirection.SHORT and not signal.take_profit < price < signal.stop_loss:
        return None
    return _OpenPosition(signal, bar.opened_at, price, bar.bar_id)


def _bar_gapped_beyond_stop(signal: BacktestSignal, bar: MarketBarEvidence) -> bool:
    return (
        signal.direction is BacktestDirection.LONG and bar.open < signal.stop_loss
    ) or (
        signal.direction is BacktestDirection.SHORT and bar.open > signal.stop_loss
    )


def _exit_for_bar(
    position: _OpenPosition,
    bar: MarketBarEvidence,
    *,
    maximum_holding: bool,
) -> tuple[float, BacktestExitReason] | None:
    signal = position.signal
    if signal.direction is BacktestDirection.LONG:
        if bar.open <= signal.stop_loss:
            return bar.open, BacktestExitReason.GAP_STOP
        if bar.open >= signal.take_profit:
            return bar.open, BacktestExitReason.GAP_TARGET
        stop_hit = bar.low <= signal.stop_loss
        target_hit = bar.high >= signal.take_profit
    else:
        if bar.open >= signal.stop_loss:
            return bar.open, BacktestExitReason.GAP_STOP
        if bar.open <= signal.take_profit:
            return bar.open, BacktestExitReason.GAP_TARGET
        stop_hit = bar.high >= signal.stop_loss
        target_hit = bar.low <= signal.take_profit
    if stop_hit:
        return signal.stop_loss, BacktestExitReason.STOP_LOSS
    if target_hit:
        return signal.take_profit, BacktestExitReason.TAKE_PROFIT
    if maximum_holding:
        return bar.close, BacktestExitReason.MAXIMUM_HOLDING_PERIOD
    return None


def _close_trade(
    position: _OpenPosition,
    bar: MarketBarEvidence,
    exit_price: float,
    reason: BacktestExitReason,
    request: BacktestRequest,
    *,
    index: int,
) -> BacktestTrade:
    signal = position.signal
    sign = 1.0 if signal.direction is BacktestDirection.LONG else -1.0
    gross_return = sign * (exit_price - position.entry_price) / position.entry_price * 100
    sizing = signal.position_sizing
    size = None if sizing is None else sizing.position_size
    net_return, gross_money, net_money = _cost_adjusted_results(
        request,
        direction=signal.direction,
        entry=position.entry_price,
        exit=exit_price,
        position_size=size,
    )
    trade_id = "bt-" + sha256(
        f"{signal.signal_at.isoformat()}|{position.entry_bar_id}|{bar.bar_id}|{index}".encode()
    ).hexdigest()[:24]
    return BacktestTrade(
        trade_id=trade_id,
        direction=signal.direction,
        signal_at=signal.signal_at,
        entered_at=position.entered_at,
        exited_at=bar.closed_at,
        entry_price=position.entry_price,
        exit_price=exit_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        exit_reason=reason,
        holding_bars=position.holding_bars,
        holding_seconds=max(1, int((bar.closed_at - position.entered_at).total_seconds())),
        gross_return_pct=gross_return,
        net_return_pct=net_return,
        gross_monetary_pnl=gross_money,
        net_monetary_pnl=net_money,
        position_size=size,
        size_unit=None if sizing is None else sizing.size_unit,
        costs_applied=net_return is not None,
        entry_bar_id=position.entry_bar_id,
        exit_bar_id=bar.bar_id,
        signal_evidence_ids=signal.evidence_ids,
    )


def _cost_adjusted_results(request, *, direction, entry, exit, position_size):
    costs = request.verified_costs
    context = request.sizing_context
    gross_money = _monetary_pnl(context, direction, entry, exit, position_size)
    if costs is None:
        return None, gross_money, None
    spread = float(costs.spread_price or 0.0)
    slippage = float(costs.slippage_price_per_side or 0.0)
    sign = 1.0 if direction is BacktestDirection.LONG else -1.0
    adjusted_entry = entry + sign * (spread / 2 + slippage)
    adjusted_exit = exit - sign * (spread / 2 + slippage)
    net_return = sign * (adjusted_exit - adjusted_entry) / adjusted_entry * 100
    commission = float(costs.commission_per_size_round_trip or 0.0)
    if commission:
        if context is None or position_size is None:
            return None, gross_money, None
        commission_money = commission * position_size
        net_return -= commission_money / float(context.account.equity) * 100
    net_money = _monetary_pnl(context, direction, adjusted_entry, adjusted_exit, position_size)
    if net_money is not None:
        net_money -= commission * (position_size or 0.0)
    return net_return, gross_money, net_money


def _monetary_pnl(context, direction, entry, exit, position_size):
    if context is None or position_size is None:
        return None
    spec = context.instrument
    if spec.tick_size is None or spec.tick_value is None:
        return None
    sign = 1.0 if direction is BacktestDirection.LONG else -1.0
    value = sign * (exit - entry) / float(spec.tick_size) * float(spec.tick_value) * position_size
    if spec.tick_value_currency != context.account.currency:
        conversion = context.conversion
        if conversion is None:
            return None
        if conversion.from_currency != spec.tick_value_currency or conversion.to_currency != context.account.currency:
            return None
        value *= float(conversion.rate)
    return value


def _series_metrics(returns: list[float], *, basis: str) -> ReturnSeriesMetrics:
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    breakeven = len(returns) - len(wins) - len(losses)
    available = MetricStatus.AVAILABLE
    undefined = MetricStatus.UNDEFINED
    insufficient = MetricStatus.INSUFFICIENT_SAMPLE
    if returns:
        win_rate = BacktestMetric(status=available, value=len(wins) / len(returns) * 100, unit="percent")
        profit = BacktestMetric(status=available, value=sum(wins), unit="return_percent_sum")
        loss = BacktestMetric(status=available, value=sum(losses), unit="return_percent_sum")
        average_win = BacktestMetric(status=available if wins else undefined, value=fmean(wins) if wins else None, unit="percent", reason=None if wins else "no winning trades")
        average_loss = BacktestMetric(status=available if losses else undefined, value=fmean(losses) if losses else None, unit="percent", reason=None if losses else "no losing trades")
        expectancy = BacktestMetric(status=available, value=fmean(returns), unit="percent_per_trade")
        total = (math.prod(1 + value / 100 for value in returns) - 1) * 100
        total_return = BacktestMetric(status=available, value=total, unit="percent")
    else:
        reason = "no completed trades"
        win_rate = BacktestMetric(status=undefined, unit="percent", reason=reason)
        profit = BacktestMetric(status=undefined, unit="return_percent_sum", reason=reason)
        loss = BacktestMetric(status=undefined, unit="return_percent_sum", reason=reason)
        average_win = BacktestMetric(status=undefined, unit="percent", reason=reason)
        average_loss = BacktestMetric(status=undefined, unit="percent", reason=reason)
        expectancy = BacktestMetric(status=undefined, unit="percent_per_trade", reason=reason)
        total_return = BacktestMetric(status=undefined, unit="percent", reason=reason)
    if losses:
        profit_factor = BacktestMetric(status=available, value=sum(wins) / abs(sum(losses)), unit="ratio")
    else:
        profit_factor = BacktestMetric(status=undefined, unit="ratio", reason="profit factor requires at least one losing trade")
    curve_values = _equity_curve_values(returns)
    maximum_drawdown = BacktestMetric(
        status=available if curve_values else undefined,
        value=abs(min((item[1] for item in curve_values), default=0.0)) if curve_values else None,
        unit="percent",
        reason=None if curve_values else "no completed trades",
    )
    if len(returns) < 30:
        sharpe = BacktestMetric(status=insufficient, unit="per_trade_ratio", reason="at least 30 non-zero-variance trade returns are required")
    elif math.isclose(pstdev(returns), 0.0):
        sharpe = BacktestMetric(status=undefined, unit="per_trade_ratio", reason="trade-return variance is zero")
    else:
        sharpe = BacktestMetric(status=available, value=fmean(returns) / pstdev(returns) * math.sqrt(len(returns)), unit="per_trade_ratio")
    return ReturnSeriesMetrics(
            basis=basis,
            winning_trades=len(wins),
            losing_trades=len(losses),
            breakeven_trades=breakeven,
            win_rate=win_rate,
            profit=profit,
            loss=loss,
            profit_factor=profit_factor,
            average_win=average_win,
            average_loss=average_loss,
            expectancy=expectancy,
            maximum_drawdown=maximum_drawdown,
            total_return=total_return,
            consecutive_wins=_maximum_streak(returns, positive=True),
            consecutive_losses=_maximum_streak(returns, positive=False),
            sharpe_ratio=sharpe,
    )


def _metrics(trades: tuple[BacktestTrade, ...], *, include_net: bool = False):
    gross_returns = [float(item.gross_return_pct) for item in trades]
    gross = _series_metrics(gross_returns, basis="gross_returns")
    net = None
    if include_net:
        net_returns = [float(item.net_return_pct) for item in trades if item.net_return_pct is not None]
        if len(net_returns) == len(trades):
            net = _series_metrics(net_returns, basis="net_returns")
    holding = BacktestMetric(
        status=MetricStatus.AVAILABLE if trades else MetricStatus.UNDEFINED,
        value=fmean(item.holding_seconds for item in trades) if trades else None,
        unit="seconds",
        reason=None if trades else "no completed trades",
    )
    risk_rewards = [
        (1.0 if item.direction is BacktestDirection.LONG else -1.0)
        * (item.exit_price - item.entry_price)
        / abs(item.entry_price - item.stop_loss)
        for item in trades
        if item.entry_price != item.stop_loss
    ]
    realized_rr = BacktestMetric(
        status=MetricStatus.AVAILABLE if risk_rewards else MetricStatus.UNDEFINED,
        value=fmean(risk_rewards) if risk_rewards else None,
        unit="ratio",
        reason=None if risk_rewards else "no bounded realized trade risk",
    )
    metrics = PerformanceMetrics(
        total_trades=len(trades),
        winning_trades=gross.winning_trades,
        losing_trades=gross.losing_trades,
        breakeven_trades=gross.breakeven_trades,
        win_rate=gross.win_rate,
        gross_profit=gross.profit,
        gross_loss=gross.loss,
        profit_factor=gross.profit_factor,
        average_win=gross.average_win,
        average_loss=gross.average_loss,
        expectancy=gross.expectancy,
        maximum_drawdown=gross.maximum_drawdown,
        total_return=gross.total_return,
        average_holding_duration=holding,
        consecutive_wins=gross.consecutive_wins,
        consecutive_losses=gross.consecutive_losses,
        realized_risk_reward=realized_rr,
        sharpe_ratio=gross.sharpe_ratio,
        gross=gross,
        net=net,
    )
    return metrics, _equity_curve(trades, net_basis=False), (
        _equity_curve(trades, net_basis=True) if net is not None else ()
    )


def _equity_curve_values(returns):
    value = 1.0
    peak = 1.0
    points = []
    for result in returns:
        value *= 1 + result / 100
        peak = max(peak, value)
        points.append(((value - 1) * 100, (value / peak - 1) * 100))
    return tuple(points)


def _equity_curve(trades, *, net_basis):
    returns = [
        float(trade.net_return_pct if net_basis else trade.gross_return_pct)
        for trade in trades
    ]
    values = _equity_curve_values(returns)
    return tuple(
        EquityPoint(timestamp=trade.exited_at, cumulative_return_pct=value[0], drawdown_pct=value[1])
        for trade, value in zip(trades, values, strict=True)
    )


def _maximum_streak(returns, *, positive):
    best = current = 0
    for value in returns:
        matches = value > 0 if positive else value < 0
        current = current + 1 if matches else 0
        best = max(best, current)
    return best


def _empty_metrics():
    metrics, _, _ = _metrics(())
    return metrics


def _assess_costs(request: BacktestRequest, evaluated_at: datetime) -> BacktestCostAssessment:
    costs = request.verified_costs
    component_names = (
        "spread_price",
        "slippage_price_per_side",
        "commission_per_size_round_trip",
    )
    if costs is None:
        return BacktestCostAssessment(
            status=CostVerificationStatus.UNAVAILABLE,
            missing_components=component_names,
            assumptions=("No trading-cost values were supplied; net performance is unavailable.",),
        )
    values = {name: getattr(costs, name) for name in component_names}
    missing = tuple(name for name, value in values.items() if value is None)
    status = CostVerificationStatus.VERIFIED
    assumptions: tuple[str, ...] = (
        "All supplied trading-cost components are treated as fixed per the verified source.",
    )
    if costs.verified_at > evaluated_at:
        status = CostVerificationStatus.INVALID
        assumptions = ("The cost verification timestamp is later than the evaluation time.",)
    elif missing:
        status = CostVerificationStatus.INCOMPLETE
        assumptions = ("Missing cost components are not inferred as zero.",)
    return BacktestCostAssessment(
        status=status,
        source_id=costs.source_id,
        verified_at=costs.verified_at,
        spread_price=costs.spread_price,
        slippage_price_per_side=costs.slippage_price_per_side,
        commission_per_size_round_trip=costs.commission_per_size_round_trip,
        missing_components=missing,
        assumptions=assumptions,
    )


def _base_report(request, evaluated_at, status, quality, provenance, warnings, limitations):
    return BacktestReport(
        status=status,
        symbol=request.symbol,
        timeframe=request.timeframe,
        evaluated_at=_utc(evaluated_at),
        data_quality=quality,
        provenance=provenance,
        parameters=request.parameters,
        cost_basis="gross_only",
        cost_assessment=_assess_costs(request, _utc(evaluated_at)),
        metrics=_empty_metrics(),
        warnings=tuple(warnings),
        limitations=tuple(limitations),
    )


def _unavailable_report(request, *, evaluated_at, warning, limitation):
    return _base_report(
        request,
        evaluated_at,
        BacktestStatus.UNAVAILABLE,
        BacktestDataQuality(
            validated_bar_count=0,
            completed_bar_count=0,
            provisional_bar_count=0,
            duplicate_timestamp_count=0,
            missing_interval_count=0,
            is_ordered=True,
        ),
        None,
        (warning,),
        (limitation,),
    )


def _utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require a timezone")
    return value.astimezone(UTC)


@lru_cache
def get_backtesting_service() -> BacktestingService:
    """Compose the read-only cached-evidence backtest boundary."""
    from app.catalogue.store import InstrumentCatalogueSchemaError, read_catalogue_snapshot
    from app.services.friday_core import get_friday_core
    from app.services.instrument_catalogue import DEFAULT_CATALOGUE_CACHE_PATH

    try:
        catalogue_entries = read_catalogue_snapshot(DEFAULT_CATALOGUE_CACHE_PATH)
    except (InstrumentCatalogueSchemaError, OSError, ValueError):
        catalogue_entries = ()
    return BacktestingService(
        evidence_owner=get_friday_core(),
        resolver=ResearchRequestService(catalogue_entries=catalogue_entries),
    )
