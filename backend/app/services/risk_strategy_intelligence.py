"""Deterministic, non-authorizing risk and conditional-strategy research."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, ROUND_FLOOR

from app.evidence.models import MarketSeriesEvidence
from app.models.derived_features import FeatureStatus, FeatureType
from app.models.liquidity_intelligence import (
    FairValueGapDirection,
    FairValueGapState,
    LiquidityAnalysisStatus,
    LiquidityIntelligenceReport,
    OrderBlockDirection,
    OrderBlockState,
    StructureTrend,
    TimeframeAlignmentState,
)
from app.models.macro_intelligence import MacroIntelligenceReport
from app.models.market_series import MarketBarState
from app.models.news_intelligence import NewsEconomicIntelligenceReport
from app.models.risk_strategy_intelligence import (
    CalculationStatus,
    EntryZone,
    EvidenceSufficiency,
    PositionSizingResult,
    RiskRewardAssessment,
    RiskSizingContext,
    RiskStrategyIntelligenceReport,
    RiskStrategyStatus,
    ScenarioDirection,
    ScenarioStatus,
    StrategyQualityAssessment,
    StrategyQualityFactor,
    StrategyScenario,
    VolatilityAssessment,
    VolatilityRegime,
)
from app.models.technical_analysis import (
    TechnicalAnalysisReport,
    TechnicalTimeframeAnalysis,
    TrendDirection,
)


QUALITY_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("multi_timeframe_agreement", 15),
    ("technical_confirmation", 20),
    ("liquidity_confirmation", 15),
    ("macro_context", 10),
    ("news_risk_context", 10),
    ("entry_invalidation_clarity", 15),
    ("risk_reward_suitability", 15),
)


class RiskStrategyIntelligenceService:
    """Combine validated research outputs without creating an order instruction."""

    def calculate_position_size(
        self,
        risk_reward: RiskRewardAssessment,
        context: RiskSizingContext | None,
        *,
        evaluated_at: datetime,
    ) -> PositionSizingResult:
        """Calculate a bounded size only from explicit verified inputs."""
        return _position_sizing(
            risk_reward,
            context,
            evaluated_at=_utc(evaluated_at),
        )

    def analyze(
        self,
        *,
        subject: str,
        technical: TechnicalAnalysisReport | None,
        liquidity: LiquidityIntelligenceReport | None,
        macro: MacroIntelligenceReport | None,
        news: NewsEconomicIntelligenceReport | None,
        market_series: tuple[MarketSeriesEvidence, ...] = (),
        sizing_context: RiskSizingContext | None = None,
        evaluated_at: datetime | None = None,
    ) -> RiskStrategyIntelligenceReport:
        evaluation = _utc(evaluated_at or datetime.now(UTC))
        technical_analysis = _preferred_technical(technical)
        if technical_analysis is not None and (
            technical_analysis.retrieved_at > evaluation
            or (
                technical_analysis.coverage_end is not None
                and technical_analysis.coverage_end > evaluation
            )
        ):
            technical_analysis = None
        liquidity_analysis = _preferred_liquidity(liquidity, technical_analysis)
        if liquidity_analysis is not None and liquidity_analysis.evaluated_at > evaluation:
            liquidity_analysis = None
        if macro is not None and macro.evaluated_at > evaluation:
            macro = None
        if news is not None and news.evaluated_at > evaluation:
            news = None
        series = _preferred_series(market_series, technical_analysis)
        volatility = _volatility(technical_analysis, series, evaluation)

        scenarios = tuple(
            self._scenario(
                direction,
                technical_analysis=technical_analysis,
                liquidity=liquidity,
                liquidity_analysis=liquidity_analysis,
                macro=macro,
                news=news,
                volatility=volatility,
                sizing_context=sizing_context,
                evaluated_at=evaluation,
            )
            for direction in (
                ScenarioDirection.BULLISH,
                ScenarioDirection.BEARISH,
                ScenarioDirection.NEUTRAL,
            )
        )
        warnings = tuple(
            dict.fromkeys(
                warning
                for source in (
                    (() if technical_analysis is None else technical_analysis.warnings),
                    (() if liquidity_analysis is None else liquidity_analysis.warnings),
                    (() if macro is None else macro.warnings),
                    (() if news is None else news.warnings),
                )
                for warning in source
            )
        )
        missing: list[str] = []
        if technical_analysis is None:
            missing.append("validated_technical_analysis")
        if liquidity_analysis is None:
            missing.append("validated_liquidity_analysis")
        if macro is None:
            missing.append("validated_macro_context")
        if news is None:
            missing.append("validated_news_and_event_context")
        if sizing_context is None:
            missing.append("verified_account_and_instrument_sizing_context")
        conditional = sum(item.status is ScenarioStatus.CONDITIONAL for item in scenarios)
        stale = bool(
            liquidity_analysis is not None
            and liquidity_analysis.status is LiquidityAnalysisStatus.STALE
        )
        if stale:
            status = RiskStrategyStatus.STALE
        elif conditional and not missing:
            status = RiskStrategyStatus.AVAILABLE
        elif conditional:
            status = RiskStrategyStatus.PARTIAL
        else:
            status = RiskStrategyStatus.INSUFFICIENT_DATA
        return RiskStrategyIntelligenceReport(
            subject=subject,
            evaluated_at=evaluation,
            status=status,
            volatility=volatility,
            scenarios=scenarios,
            warnings=warnings,
            missing_inputs=tuple(missing),
            limitations=(
                "Scenarios are conditional research constructs, not recommendations or orders.",
                "Quality scores measure evidence completeness under documented weights; they are not win probabilities.",
                "No broker, account, position, order book, or execution system was accessed.",
            ),
        )

    def _scenario(
        self,
        direction: ScenarioDirection,
        *,
        technical_analysis: TechnicalTimeframeAnalysis | None,
        liquidity: LiquidityIntelligenceReport | None,
        liquidity_analysis,
        macro: MacroIntelligenceReport | None,
        news: NewsEconomicIntelligenceReport | None,
        volatility: VolatilityAssessment,
        sizing_context: RiskSizingContext | None,
        evaluated_at: datetime,
    ) -> StrategyScenario:
        if direction is ScenarioDirection.NEUTRAL:
            quality = _quality(
                direction,
                technical_analysis,
                liquidity,
                liquidity_analysis,
                macro,
                news,
                None,
            )
            return StrategyScenario(
                direction=direction,
                status=ScenarioStatus.NO_TRADE,
                entry_conditions=(
                    "Remain neutral while directional evidence conflicts or entry and invalidation are unclear.",
                ),
                invalidation_conditions=(
                    "Reassess only after fresh evidence establishes a bounded directional setup.",
                ),
                risk_reward=_unavailable_risk_reward("Neutral scenario has no proposed entry."),
                position_sizing=_unavailable_sizing("Neutral scenario has no position."),
                quality=quality,
                supporting_evidence=_neutral_support(technical_analysis, liquidity_analysis),
                missing_evidence=_missing_evidence(technical_analysis, liquidity_analysis, macro, news),
                expiration_conditions=("Any material evidence update requires a new assessment.",),
                limitations=("No-trade is an explicit valid research outcome.",),
            )

        entry = _entry_zone(direction, technical_analysis, liquidity_analysis)
        stop = _stop_candidate(direction, entry, technical_analysis, liquidity_analysis)
        targets = _target_candidates(direction, entry, technical_analysis, liquidity_analysis)
        risk_reward = _risk_reward(
            direction,
            entry,
            stop,
            targets,
            sizing_context,
            volatility,
        )
        sizing = _position_sizing(
            risk_reward,
            sizing_context,
            evaluated_at=evaluated_at,
        )
        quality = _quality(
            direction,
            technical_analysis,
            liquidity,
            liquidity_analysis,
            macro,
            news,
            risk_reward,
        )
        status = (
            ScenarioStatus.CONDITIONAL
            if entry is not None and stop is not None
            else ScenarioStatus.INSUFFICIENT_DATA
        )
        evidence, conflicts = _directional_evidence(
            direction,
            technical_analysis,
            liquidity_analysis,
        )
        missing = list(_missing_evidence(technical_analysis, liquidity_analysis, macro, news))
        if entry is None:
            missing.append("evidence_bound_entry_zone")
        if not targets:
            missing.append("evidence_bound_target_zone")
        if sizing.status is not CalculationStatus.AVAILABLE:
            missing.extend(sizing.missing_inputs)
        return StrategyScenario(
            direction=direction,
            status=status,
            entry_zone=entry,
            entry_conditions=(
                f"Require fresh {direction.value} confirmation at the cited evidence-bound zone.",
                "Recalculate after any new completed candle changes structure or liquidity state.",
            ),
            invalidation_conditions=(
                f"A completed candle beyond {stop} invalidates this scenario."
                if stop is not None
                else "No bounded invalidation level is available; the scenario remains insufficient.",
            ),
            stop_loss_candidate=stop,
            target_candidates=targets,
            risk_reward=risk_reward,
            position_sizing=sizing,
            quality=quality,
            supporting_evidence=evidence,
            conflicting_evidence=conflicts,
            missing_evidence=tuple(dict.fromkeys(missing)),
            expiration_conditions=(
                "The cited structure or liquidity candidate is invalidated.",
                "Source evidence becomes stale or its fingerprint changes.",
                "A verified high-impact event requires reassessment.",
            ),
            limitations=(
                "Pattern evidence does not guarantee direction or target attainment.",
                "No scenario grants production authorization, submission, or execution authority.",
            ),
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation timestamps must include a timezone")
    return value.astimezone(UTC)


def _preferred_technical(
    report: TechnicalAnalysisReport | None,
) -> TechnicalTimeframeAnalysis | None:
    if report is None or not report.analyses:
        return None
    return report.analyses[0]


def _preferred_liquidity(report, technical):
    if report is None or not report.analyses:
        return None
    if technical is not None:
        matching = next(
            (item for item in report.analyses if item.timeframe == technical.timeframe),
            None,
        )
        if matching is not None:
            return matching
    return report.analyses[0]


def _preferred_series(series, technical):
    candidates = tuple(
        item
        for item in series
        if technical is None or item.timeframe == technical.timeframe
    )
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.series_id)[0]


def _feature(analysis, feature_type: FeatureType, evaluated_at: datetime) -> float | None:
    if analysis is None:
        return None
    item = next(
        (
            value
            for value in analysis.indicators
            if value.feature_type is feature_type and value.status is FeatureStatus.AVAILABLE
            and value.as_of is not None
            and value.as_of <= evaluated_at
        ),
        None,
    )
    return None if item is None else item.value


def _volatility(technical, series, evaluated_at) -> VolatilityAssessment:
    atr = _feature(technical, FeatureType.ATR, evaluated_at)
    realized = _feature(technical, FeatureType.ROLLING_STD, evaluated_at)
    completed = () if series is None else tuple(
        bar
        for bar in series.bars
        if bar.state is MarketBarState.COMPLETED
        and bar.closed_at <= evaluated_at
        and bar.received_at <= evaluated_at
    )
    latest = completed[-1] if completed else None
    latest_price = None if latest is None else latest.close
    atr_percentage = (
        None if atr is None or latest_price in {None, 0} else atr / abs(latest_price) * 100
    )
    if atr_percentage is None:
        regime = VolatilityRegime.UNKNOWN
    elif atr_percentage < 0.5:
        regime = VolatilityRegime.LOW
    elif atr_percentage <= 1.5:
        regime = VolatilityRegime.NORMAL
    else:
        regime = VolatilityRegime.HIGH
    range_ratio = (
        None if latest is None or atr in {None, 0} else (latest.high - latest.low) / atr
    )
    warnings = () if series is None or not series.continuity.missing_interval_count else (
        "MISSING_INTERVALS_PRESENT",
    )
    available = atr is not None or realized is not None
    return VolatilityAssessment(
        status=CalculationStatus.AVAILABLE if available else CalculationStatus.UNAVAILABLE,
        timeframe=None if technical is None else technical.timeframe,
        atr=atr,
        realized_volatility=realized,
        atr_percentage_of_price=atr_percentage,
        latest_range_to_atr=range_ratio,
        regime=regime,
        range_expansion=None if range_ratio is None else range_ratio > 1.5,
        stop_suitability=(
            "Stop suitability requires a bounded scenario stop distance."
            if atr is not None
            else "ATR is unavailable; volatility-relative stop suitability cannot be assessed."
        ),
        warnings=warnings,
    )


def _entry_zone(direction, technical, liquidity) -> EntryZone | None:
    if liquidity is not None:
        blocks = [
            item
            for item in liquidity.order_block_candidates
            if item.state is not OrderBlockState.INVALIDATED
            and (
                (direction is ScenarioDirection.BULLISH and item.direction is OrderBlockDirection.BULLISH)
                or (direction is ScenarioDirection.BEARISH and item.direction is OrderBlockDirection.BEARISH)
            )
        ]
        if blocks:
            item = blocks[-1]
            return EntryZone(
                lower=item.lower_boundary,
                upper=item.upper_boundary,
                reference_price=(item.lower_boundary + item.upper_boundary) / 2,
                basis=f"{direction.value}_order_block_candidate",
                evidence_ids=(item.candidate_id, item.associated_break_id),
            )
        gaps = [
            item
            for item in liquidity.fair_value_gaps
            if item.state is not FairValueGapState.FULLY_FILLED
            and (
                (direction is ScenarioDirection.BULLISH and item.direction is FairValueGapDirection.BULLISH)
                or (direction is ScenarioDirection.BEARISH and item.direction is FairValueGapDirection.BEARISH)
            )
        ]
        if gaps:
            item = gaps[-1]
            return EntryZone(
                lower=item.lower_boundary,
                upper=item.upper_boundary,
                reference_price=(item.lower_boundary + item.upper_boundary) / 2,
                basis=f"{direction.value}_fair_value_gap_candidate",
                evidence_ids=(item.gap_id,),
            )
    if technical is not None:
        levels = (
            technical.support_candidates
            if direction is ScenarioDirection.BULLISH
            else technical.resistance_candidates
        )
        if levels:
            item = levels[-1]
            return EntryZone(
                lower=item.price,
                upper=item.price,
                reference_price=item.price,
                basis=item.basis,
                evidence_ids=(item.source_bar_id,),
            )
    return None


def _stop_candidate(direction, entry, technical, liquidity):
    if entry is None:
        return None
    boundary = entry.lower if direction is ScenarioDirection.BULLISH else entry.upper
    if entry.lower != entry.upper:
        return boundary
    if technical is not None:
        levels = (
            technical.support_candidates
            if direction is ScenarioDirection.BULLISH
            else technical.resistance_candidates
        )
        alternatives = (
            [item.price for item in levels if item.price < entry.reference_price]
            if direction is ScenarioDirection.BULLISH
            else [item.price for item in levels if item.price > entry.reference_price]
        )
        if alternatives:
            return max(alternatives) if direction is ScenarioDirection.BULLISH else min(alternatives)
    return None


def _target_candidates(direction, entry, technical, liquidity) -> tuple[float, ...]:
    if entry is None:
        return ()
    values: list[float] = []
    if technical is not None:
        levels = (
            technical.resistance_candidates
            if direction is ScenarioDirection.BULLISH
            else technical.support_candidates
        )
        values.extend(item.price for item in levels)
    if liquidity is not None:
        side = "buy_side" if direction is ScenarioDirection.BULLISH else "sell_side"
        values.extend(item.price for item in liquidity.liquidity_levels if item.side.value == side)
    filtered = (
        (value for value in values if value > entry.reference_price)
        if direction is ScenarioDirection.BULLISH
        else (value for value in values if value < entry.reference_price)
    )
    return tuple(sorted(set(filtered), reverse=direction is ScenarioDirection.BEARISH)[:3])


def _risk_reward(direction, entry, stop, targets, sizing_context, volatility):
    if entry is None or stop is None:
        return _unavailable_risk_reward("Entry and invalidation are not both available.")
    distance = abs(entry.reference_price - stop)
    if distance <= 0:
        return _unavailable_risk_reward("Entry zone and invalidation do not define positive risk distance.")
    target = targets[0] if targets else None
    reward = None if target is None else abs(target - entry.reference_price)
    pip_size = None if sizing_context is None else sizing_context.instrument.pip_size
    atr_multiple = None if volatility.atr in {None, 0} else distance / volatility.atr
    stop_suitability = (
        "ATR is unavailable; volatility-relative stop suitability cannot be assessed."
        if atr_multiple is None
        else "Stop distance is tighter than 0.5 ATR and may be sensitive to ordinary range noise."
        if atr_multiple < 0.5
        else "Stop distance is inside the descriptive 0.5 to 3 ATR policy band."
        if atr_multiple <= 3
        else "Stop distance is wider than 3 ATR and requires explicit risk review."
    )
    return RiskRewardAssessment(
        status=CalculationStatus.AVAILABLE if reward and reward > 0 else CalculationStatus.UNAVAILABLE,
        entry_price=entry.reference_price,
        stop_loss=stop,
        invalidation_level=stop,
        target_price=target,
        stop_distance_price=distance,
        stop_distance_pips=None if pip_size is None else distance / pip_size,
        stop_distance_atr_multiple=atr_multiple,
        stop_suitability=stop_suitability,
        potential_reward=reward,
        ratio=None if reward is None else reward / distance,
        limitations=(() if reward else ("No evidence-bound target is available.",)),
    )


def _unavailable_risk_reward(reason: str) -> RiskRewardAssessment:
    return RiskRewardAssessment(
        status=CalculationStatus.UNAVAILABLE,
        stop_suitability="Unavailable because positive evidence-bound stop distance is missing.",
        limitations=(reason,),
    )


def _position_sizing(risk_reward, context, *, evaluated_at):
    if context is None:
        return _unavailable_sizing(
            "Verified account equity, risk percentage, and instrument contract facts are unavailable."
        )
    missing: list[str] = []
    spec = context.instrument
    if risk_reward.stop_distance_price is None:
        missing.append("positive_stop_distance")
    for name, value in (
        ("tick_size", spec.tick_size),
        ("tick_value", spec.tick_value),
        ("minimum_size", spec.minimum_size),
        ("maximum_size", spec.maximum_size),
        ("size_step", spec.size_step),
    ):
        if value is None:
            missing.append(name)
    if context.account.observed_at > evaluated_at or spec.verified_at > evaluated_at:
        missing.append("non_future_verified_input_timestamps")
    conversion = 1.0
    if spec.tick_value_currency != context.account.currency:
        item = context.conversion
        if (
            item is None
            or item.from_currency != spec.tick_value_currency
            or item.to_currency != context.account.currency
            or item.observed_at > evaluated_at
        ):
            missing.append("verified_tick_value_to_account_currency_conversion")
        else:
            conversion = item.rate
    if missing:
        result = _unavailable_sizing("Essential verified sizing inputs are missing or invalid.")
        return result.model_copy(update={"missing_inputs": tuple(missing)})
    assert risk_reward.stop_distance_price is not None
    assert spec.tick_size is not None and spec.tick_value is not None
    assert spec.minimum_size is not None and spec.maximum_size is not None and spec.size_step is not None
    risk_amount = context.account.equity * context.account.risk_percentage / 100
    loss_per_size = risk_reward.stop_distance_price / spec.tick_size * spec.tick_value * conversion
    if loss_per_size <= 0:
        return _unavailable_sizing("Verified contract inputs produced a non-positive loss per size.")
    raw = Decimal(str(risk_amount / loss_per_size))
    step = Decimal(str(spec.size_step))
    rounded = float((raw / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    rounded = min(rounded, spec.maximum_size)
    if rounded < spec.minimum_size:
        result = _unavailable_sizing("Calculated size is below the verified minimum size.")
        return result.model_copy(update={"risk_amount": risk_amount})
    units = None if spec.contract_size is None else rounded * spec.contract_size
    transaction_cost = None
    if context.verified_spread is not None or context.verified_commission_per_size is not None:
        spread_cost = (
            0.0
            if context.verified_spread is None
            else context.verified_spread / spec.tick_size * spec.tick_value * conversion * rounded
        )
        commission = (context.verified_commission_per_size or 0.0) * rounded
        transaction_cost = spread_cost + commission
    return PositionSizingResult(
        status=CalculationStatus.AVAILABLE,
        account_currency=context.account.currency,
        risk_percentage=context.account.risk_percentage,
        risk_amount=risk_amount,
        position_size=rounded,
        size_unit=spec.size_unit,
        units=units,
        monetary_loss_per_size=loss_per_size,
        estimated_transaction_cost=transaction_cost,
        assumptions=(
            "Risk amount = verified equity × risk percentage / 100.",
            "Size is rounded down to the verified step and capped at the verified maximum.",
            "Commission, when supplied, is stated in account currency per size unit.",
        ),
        limitations=("Sizing is research output only and cannot authorize or submit an order.",),
    )


def _unavailable_sizing(reason: str) -> PositionSizingResult:
    return PositionSizingResult(
        status=CalculationStatus.UNAVAILABLE,
        missing_inputs=(reason,),
        limitations=("No approximate position size was fabricated.",),
    )


def _quality(direction, technical, liquidity_report, liquidity, macro, news, rr):
    fractions: dict[str, tuple[float, str]] = {}
    alignment = None if liquidity_report is None else liquidity_report.alignment.state
    aligned = (
        alignment is TimeframeAlignmentState.ALIGNED_BULLISH
        if direction is ScenarioDirection.BULLISH
        else alignment is TimeframeAlignmentState.ALIGNED_BEARISH
        if direction is ScenarioDirection.BEARISH
        else alignment in {TimeframeAlignmentState.MIXED, TimeframeAlignmentState.INSUFFICIENT_DATA}
    )
    fractions["multi_timeframe_agreement"] = (
        (1.0 if aligned else 0.0) if alignment is not None else 0.0,
        "Fresh timeframe alignment matches the scenario." if aligned else "Timeframe agreement is missing or conflicting.",
    )
    expected_trend = (
        TrendDirection.BULLISH
        if direction is ScenarioDirection.BULLISH
        else TrendDirection.BEARISH
        if direction is ScenarioDirection.BEARISH
        else TrendDirection.MIXED
    )
    tech_match = technical is not None and technical.trend_direction is expected_trend
    fractions["technical_confirmation"] = (
        1.0 if tech_match else 0.5 if technical is not None else 0.0,
        "Technical trend matches the scenario." if tech_match else "Technical evidence is absent or does not fully match.",
    )
    expected_structure = (
        StructureTrend.BULLISH
        if direction is ScenarioDirection.BULLISH
        else StructureTrend.BEARISH
        if direction is ScenarioDirection.BEARISH
        else StructureTrend.RANGE
    )
    liquidity_match = liquidity is not None and liquidity.structure_trend is expected_structure
    fractions["liquidity_confirmation"] = (
        1.0 if liquidity_match else 0.5 if liquidity is not None else 0.0,
        "Price structure matches the scenario." if liquidity_match else "Liquidity structure is absent or conflicting.",
    )
    fractions["macro_context"] = (
        0.5 if macro is not None else 0.0,
        "Macro evidence is available but is not converted into an unsupported directional vote."
        if macro is not None
        else "Validated macro context is unavailable.",
    )
    event_risk = bool(news is not None and news.event_risks)
    fractions["news_risk_context"] = (
        0.5 if event_risk else 1.0 if news is not None else 0.0,
        "Verified event risk requires caution." if event_risk else "News context is available." if news is not None else "News and event context is unavailable.",
    )
    clear = rr is not None and rr.stop_distance_price is not None
    fractions["entry_invalidation_clarity"] = (
        1.0 if clear else 0.0,
        "Entry and invalidation define positive risk distance." if clear else "Entry or invalidation is unavailable.",
    )
    ratio = None if rr is None else rr.ratio
    fraction = 1.0 if ratio is not None and ratio >= 2 else 0.5 if ratio is not None and ratio >= 1 else 0.0
    fractions["risk_reward_suitability"] = (
        fraction,
        f"Risk-to-reward is {ratio:.2f}." if ratio is not None else "Risk-to-reward is unavailable.",
    )
    factors = tuple(
        StrategyQualityFactor(
            factor=name,
            weight=weight,
            score_fraction=fractions[name][0],
            points=weight * fractions[name][0],
            reason=fractions[name][1],
        )
        for name, weight in QUALITY_WEIGHTS
    )
    score = sum(item.points for item in factors)
    sufficiency = (
        EvidenceSufficiency.HIGH
        if score >= 80
        else EvidenceSufficiency.MEDIUM
        if score >= 60
        else EvidenceSufficiency.LOW
        if score >= 35
        else EvidenceSufficiency.INSUFFICIENT
    )
    return StrategyQualityAssessment(score=score, sufficiency=sufficiency, factors=factors)


def _directional_evidence(direction, technical, liquidity):
    support: list[str] = []
    conflict: list[str] = []
    expected_technical = TrendDirection.BULLISH if direction is ScenarioDirection.BULLISH else TrendDirection.BEARISH
    if technical is not None:
        target = support if technical.trend_direction is expected_technical else conflict
        target.append(f"technical:{technical.timeframe}:{technical.trend_direction.value}")
    expected_structure = StructureTrend.BULLISH if direction is ScenarioDirection.BULLISH else StructureTrend.BEARISH
    if liquidity is not None:
        target = support if liquidity.structure_trend is expected_structure else conflict
        target.append(f"liquidity:{liquidity.timeframe}:{liquidity.structure_trend.value}")
    return tuple(support), tuple(conflict)


def _neutral_support(technical, liquidity):
    values: list[str] = []
    if technical is not None and technical.trend_direction in {TrendDirection.MIXED, TrendDirection.INSUFFICIENT_DATA}:
        values.append(f"technical:{technical.timeframe}:{technical.trend_direction.value}")
    if liquidity is not None and liquidity.structure_trend in {StructureTrend.RANGE, StructureTrend.INSUFFICIENT_DATA}:
        values.append(f"liquidity:{liquidity.timeframe}:{liquidity.structure_trend.value}")
    return tuple(values)


def _missing_evidence(technical, liquidity, macro, news):
    return tuple(
        name
        for name, value in (
            ("technical_analysis", technical),
            ("liquidity_analysis", liquidity),
            ("macro_context", macro),
            ("news_and_event_context", news),
            ("verified_session_context", None),
        )
        if value is None
    )
