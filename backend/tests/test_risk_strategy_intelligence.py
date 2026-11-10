"""Offline deterministic coverage for Phase 5.6 risk and strategy research."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.agents import create_friday_core
from app.evidence import MarketSeriesStore
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.market_series import MarketBarState, MarketSeriesSource, NormalizedMarketBar, VolumeKind
from app.models.risk_strategy_intelligence import (
    AccountRiskInput,
    CalculationStatus,
    CurrencyConversionInput,
    InstrumentRiskSpecification,
    RiskRewardAssessment,
    RiskSizingContext,
    RiskStrategyStatus,
    ScenarioDirection,
    ScenarioStatus,
)
from app.models.schemas import AnalyzeRequest
from app.services.liquidity_intelligence import LiquidityIntelligenceService
from app.services.risk_strategy_intelligence import RiskStrategyIntelligenceService
from app.services.technical_analysis import TechnicalAnalysisService


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SOURCE = MarketSeriesSource(
    source_id="phase56-offline",
    display_name="Phase 5.6 Offline",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)


def _risk_reward(*, distance: float = 0.005) -> RiskRewardAssessment:
    return RiskRewardAssessment(
        status=CalculationStatus.AVAILABLE,
        entry_price=1.1000,
        stop_loss=1.1000 - distance,
        invalidation_level=1.1000 - distance,
        target_price=1.1100,
        stop_distance_price=distance,
        stop_distance_pips=distance / 0.0001,
        stop_suitability="Controlled sizing fixture.",
        potential_reward=0.0100,
        ratio=0.0100 / distance,
    )


def _spec(**updates) -> InstrumentRiskSpecification:
    values = {
        "symbol": "EURUSD",
        "asset_class": AssetClass.FOREX,
        "size_unit": "lots",
        "contract_size": 100_000.0,
        "minimum_size": 0.01,
        "maximum_size": 100.0,
        "size_step": 0.01,
        "tick_size": 0.0001,
        "tick_value": 10.0,
        "tick_value_currency": "USD",
        "pip_size": 0.0001,
        "source_id": "verified-fixture",
        "verified_at": NOW - timedelta(minutes=1),
    }
    values.update(updates)
    return InstrumentRiskSpecification(**values)


def _account(**updates) -> AccountRiskInput:
    values = {
        "equity": 10_000.0,
        "risk_percentage": 1.0,
        "currency": "USD",
        "observed_at": NOW - timedelta(minutes=1),
        "source_id": "verified-fixture",
    }
    values.update(updates)
    return AccountRiskInput(**values)


def _bars(count: int = 250) -> tuple[NormalizedMarketBar, ...]:
    bars: list[NormalizedMarketBar] = []
    for index in range(count):
        wave = (index % 10) * 0.0002
        close = 1.05 + index * 0.00025 + wave
        opened = NOW - timedelta(minutes=15 * (count - index))
        bars.append(
            NormalizedMarketBar(
                source=SOURCE,
                symbol="EURUSD",
                asset_class=AssetClass.FOREX,
                venue="GLOBAL-FX",
                timeframe="15m",
                opened_at=opened,
                closed_at=opened + timedelta(minutes=15),
                open=close - 0.0001,
                high=close + 0.0004,
                low=close - 0.0004,
                close=close,
                volume=1000 + index,
                volume_kind=VolumeKind.TICK,
                state=MarketBarState.COMPLETED,
                received_at=opened + timedelta(minutes=15),
                source_timestamp=opened.isoformat(),
            )
        )
    return tuple(bars)


def _series():
    store = MarketSeriesStore(max_series=4, max_bars_per_series=500)
    for bar in _bars():
        store.ingest(bar)
    return store.snapshot(symbol="EURUSD").series[0]


def test_risk_amount_forex_pips_position_size_and_units() -> None:
    result = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(),
        RiskSizingContext(account=_account(), instrument=_spec()),
        evaluated_at=NOW,
    )

    assert result.status is CalculationStatus.AVAILABLE
    assert result.risk_amount == pytest.approx(100.0)
    assert result.monetary_loss_per_size == pytest.approx(500.0)
    assert result.position_size == pytest.approx(0.2)
    assert result.size_unit == "lots"
    assert result.units == pytest.approx(20_000.0)


def test_size_rounds_down_to_verified_step_and_respects_maximum() -> None:
    result = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(distance=0.0033),
        RiskSizingContext(account=_account(), instrument=_spec(maximum_size=0.25)),
        evaluated_at=NOW,
    )

    assert result.status is CalculationStatus.AVAILABLE
    assert result.position_size == pytest.approx(0.25)


def test_verified_spread_and_commission_are_reported_without_being_invented() -> None:
    result = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(),
        RiskSizingContext(
            account=_account(),
            instrument=_spec(),
            verified_spread=0.0002,
            verified_commission_per_size=7.0,
        ),
        evaluated_at=NOW,
    )

    assert result.status is CalculationStatus.AVAILABLE
    assert result.estimated_transaction_cost == pytest.approx(5.4)


def test_currency_conversion_is_required_and_applied() -> None:
    context = RiskSizingContext(account=_account(currency="GBP"), instrument=_spec())
    unavailable = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(), context, evaluated_at=NOW
    )
    assert unavailable.status is CalculationStatus.UNAVAILABLE
    assert "verified_tick_value_to_account_currency_conversion" in unavailable.missing_inputs

    conversion = CurrencyConversionInput(
        from_currency="USD",
        to_currency="GBP",
        rate=0.8,
        observed_at=NOW - timedelta(minutes=1),
        source_id="verified-fixture",
    )
    available = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(),
        context.model_copy(update={"conversion": conversion}),
        evaluated_at=NOW,
    )
    assert available.status is CalculationStatus.AVAILABLE
    assert available.monetary_loss_per_size == pytest.approx(400.0)
    assert available.position_size == pytest.approx(0.25)


def test_invalid_or_incomplete_contract_specification_fails_closed() -> None:
    with pytest.raises(ValidationError):
        _spec(tick_value=None)
    with pytest.raises(ValidationError):
        _spec(minimum_size=1.0, maximum_size=0.5)

    result = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(),
        RiskSizingContext(
            account=_account(),
            instrument=_spec(
                tick_size=None,
                tick_value=None,
                tick_value_currency=None,
            ),
        ),
        evaluated_at=NOW,
    )
    assert result.status is CalculationStatus.UNAVAILABLE
    assert result.position_size is None


def test_real_technical_and_liquidity_reports_create_three_conditional_outcomes() -> None:
    series = _series()
    technical = TechnicalAnalysisService(swing_window=1).analyze((series,))
    liquidity = LiquidityIntelligenceService(swing_window=1).analyze(
        (series,), evaluated_at=NOW
    )
    report = RiskStrategyIntelligenceService().analyze(
        subject="EURUSD",
        technical=technical,
        liquidity=liquidity,
        macro=None,
        news=None,
        market_series=(series,),
        evaluated_at=NOW,
    )

    assert tuple(item.direction for item in report.scenarios) == (
        ScenarioDirection.BULLISH,
        ScenarioDirection.BEARISH,
        ScenarioDirection.NEUTRAL,
    )
    assert report.scenarios[-1].status is ScenarioStatus.NO_TRADE
    assert report.scenarios[-1].position_sizing.status is CalculationStatus.UNAVAILABLE
    assert all(item.quality.interpretation.startswith("deterministic") for item in report.scenarios)
    assert report.volatility.status is CalculationStatus.AVAILABLE
    assert report.volatility.atr is not None
    assert report.volatility.realized_volatility is not None
    assert report.volatility.regime.value in {"low", "normal", "high"}
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
def test_missing_inputs_produce_neutral_no_trade_without_fabricated_values() -> None:
    service = RiskStrategyIntelligenceService()
    first = service.analyze(
        subject="UNKNOWN",
        technical=None,
        liquidity=None,
        macro=None,
        news=None,
        evaluated_at=NOW,
    )
    second = service.analyze(
        subject="UNKNOWN",
        technical=None,
        liquidity=None,
        macro=None,
        news=None,
        evaluated_at=NOW,
    )

    assert first == second
    assert first.status is RiskStrategyStatus.INSUFFICIENT_DATA
    assert all(item.entry_zone is None for item in first.scenarios)
    assert all(item.position_sizing.position_size is None for item in first.scenarios)
    assert first.scenarios[-1].status is ScenarioStatus.NO_TRADE


def test_future_technical_coverage_and_future_sizing_inputs_fail_closed() -> None:
    series = _series()
    technical = TechnicalAnalysisService(swing_window=1).analyze((series,))
    past_evaluation = NOW - timedelta(days=1)
    report = RiskStrategyIntelligenceService().analyze(
        subject="EURUSD",
        technical=technical,
        liquidity=None,
        macro=None,
        news=None,
        market_series=(series,),
        evaluated_at=past_evaluation,
    )
    sizing = RiskStrategyIntelligenceService().calculate_position_size(
        _risk_reward(),
        RiskSizingContext(
            account=_account(observed_at=NOW + timedelta(minutes=1)),
            instrument=_spec(),
        ),
        evaluated_at=NOW,
    )

    assert "validated_technical_analysis" in report.missing_inputs
    assert sizing.status is CalculationStatus.UNAVAILABLE
    assert "non_future_verified_input_timestamps" in sizing.missing_inputs


def test_stale_and_missing_evidence_remain_explicit() -> None:
    series = _series()
    technical = TechnicalAnalysisService(swing_window=1).analyze((series,))
    evaluation = NOW + timedelta(days=3)
    liquidity = LiquidityIntelligenceService(swing_window=1).analyze(
        (series,), evaluated_at=evaluation
    )
    report = RiskStrategyIntelligenceService().analyze(
        subject="EURUSD",
        technical=technical,
        liquidity=liquidity,
        macro=None,
        news=None,
        market_series=(series,),
        evaluated_at=evaluation,
    )

    assert report.status is RiskStrategyStatus.STALE
    assert "validated_macro_context" in report.missing_inputs
    assert "validated_news_and_event_context" in report.missing_inputs
    assert all(
        item.position_sizing.status is CalculationStatus.UNAVAILABLE
        for item in report.scenarios
    )


def test_agent_master_and_api_propagate_report_without_authority() -> None:
    core = create_friday_core()
    for bar in _bars():
        core.ingest_market_bar(bar)
    response = core.analyze(
        AnalyzeRequest(
            symbol="EURUSD",
            query="Assess risk, position size and bullish bearish strategy scenarios on 15m",
        )
    )

    report = response.risk_strategy_intelligence
    assert report is not None
    assert len(report.scenarios) == 3
    sentinel = next(item for item in response.agent_results if item.agent_id == "sentinel_risk_officer")
    capitol = next(item for item in response.agent_results if item.agent_id == "capitol_smart_money")
    assert "deterministic non-authorizing risk research" in sentinel.summary
    assert any("scenario=" in finding for finding in capitol.findings)
    assert "not a probability" in response.synthesis.final_response
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
