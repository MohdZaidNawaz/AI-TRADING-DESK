"""Phase 5B-1 tests for deterministic provider-neutral market features."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
import inspect
import math

import pytest
from pydantic import ValidationError

from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import EvidenceSnapshot, EvidenceStore, MarketSeriesStore, NINE_SPECIALIST_IDS
from app.models.derived_features import (
    DerivedMarketFeature,
    DerivedFeatureRequest,
    FeatureParameter,
    FeatureQualityFlag,
    FeatureStatus,
    FeatureType,
    feature_identity,
)
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.market_series import (
    MarketBarState,
    MarketSeriesSource,
    NormalizedMarketBar,
    VolumeKind,
    timeframe_seconds,
)
from app.models.schemas import AnalyzeRequest
from app.services.derived_feature_routing import DerivedFeatureRouter
from app.services.derived_features import DerivedFeatureService, feature_request
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _bar(
    index: int,
    *,
    close: float,
    open_value: float | None = None,
    high: float | None = None,
    low: float | None = None,
    symbol: str = "XAUUSD",
    venue: str | None = "TEST",
    timeframe: str = "1h",
    source_id: str = "synthetic-features",
    state: MarketBarState = MarketBarState.COMPLETED,
) -> NormalizedMarketBar:
    duration = timedelta(seconds=timeframe_seconds(timeframe))
    opened_at = MOMENT + (duration * index)
    open_value = close if open_value is None else open_value
    high = max(open_value, close) + 1.0 if high is None else high
    low = min(open_value, close) - 1.0 if low is None else low
    return NormalizedMarketBar(
        source=MarketSeriesSource(
            source_id=source_id,
            display_name="Synthetic feature history",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        ),
        symbol=symbol,
        asset_class=AssetClass.COMMODITY,
        venue=venue,
        timeframe=timeframe,
        opened_at=opened_at,
        closed_at=opened_at + duration,
        open=open_value,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        volume_kind=VolumeKind.TICK,
        state=state,
        received_at=opened_at + duration + timedelta(seconds=1),
    )


def _series(*bars: NormalizedMarketBar):
    store = MarketSeriesStore(max_series=20, max_bars_per_series=100)
    for bar in bars:
        store.ingest(bar)
    return store.snapshot(symbol=bars[0].symbol).series[0]


@pytest.mark.parametrize(
    ("feature_request_value", "bars", "expected"),
    [
        (
            feature_request(FeatureType.BAR_RANGE),
            (_bar(0, close=100, open_value=101, high=105, low=95),),
            10.0,
        ),
        (
            feature_request(FeatureType.AVERAGE_RANGE, window=3),
            (
                _bar(0, close=100, high=102, low=98),
                _bar(1, close=100, high=103, low=97),
                _bar(2, close=100, high=104, low=96),
            ),
            6.0,
        ),
        (
            feature_request(FeatureType.SIMPLE_RETURN),
            (_bar(0, close=100), _bar(1, close=110)),
            0.1,
        ),
        (
            feature_request(FeatureType.LOG_RETURN),
            (_bar(0, close=100), _bar(1, close=110)),
            math.log(1.1),
        ),
        (
            feature_request(FeatureType.SMA, window=3),
            (_bar(0, close=100), _bar(1, close=110), _bar(2, close=90)),
            100.0,
        ),
        (
            feature_request(FeatureType.ROC, window=2),
            (_bar(0, close=100), _bar(1, close=110), _bar(2, close=90)),
            -0.1,
        ),
        (
            feature_request(FeatureType.ROLLING_STD, window=2),
            (_bar(0, close=100), _bar(1, close=110), _bar(2, close=99)),
            0.1,
        ),
    ],
)
def test_exact_feature_formulas(feature_request_value, bars, expected: float) -> None:
    feature = DerivedFeatureService().calculate(_series(*bars), feature_request_value)

    assert feature.status is FeatureStatus.AVAILABLE
    assert feature.value == pytest.approx(expected)
    assert feature.method_version == "1.0.0"
    assert feature.input_first_bar_id == bars[-feature.required_bar_count].bar_id
    assert feature.input_last_bar_id == bars[-1].bar_id


def test_windowed_calculations_use_only_the_latest_exact_completed_window() -> None:
    bars = tuple(_bar(index, close=float(10 * (index + 1))) for index in range(5))
    feature = DerivedFeatureService().calculate(
        _series(*bars),
        feature_request(FeatureType.SMA, window=3),
    )

    assert feature.value == pytest.approx(40.0)
    assert feature.input_bar_count == 3
    assert feature.input_first_bar_id == bars[2].bar_id


def test_provisional_bars_are_excluded_and_reported() -> None:
    completed = _bar(0, close=100)
    provisional = _bar(1, close=200, state=MarketBarState.PROVISIONAL)
    feature = DerivedFeatureService().calculate(
        _series(completed, provisional),
        feature_request(FeatureType.BAR_RANGE),
    )

    assert feature.status is FeatureStatus.AVAILABLE
    assert feature.completed_only is True
    assert feature.input_last_bar_id == completed.bar_id
    assert FeatureQualityFlag.PROVISIONAL_EXCLUDED in feature.quality_flags


def test_insufficient_history_returns_a_typed_result_without_a_value() -> None:
    feature = DerivedFeatureService().calculate(
        _series(_bar(0, close=100)),
        feature_request(FeatureType.SMA, window=3),
    )

    assert feature.status is FeatureStatus.INSUFFICIENT_DATA
    assert feature.value is None
    assert feature.input_bar_count == 1
    assert feature.required_bar_count == 3


def test_gap_inside_required_window_prevents_calculation() -> None:
    feature = DerivedFeatureService().calculate(
        _series(_bar(0, close=100), _bar(2, close=101)),
        feature_request(FeatureType.SIMPLE_RETURN),
    )

    assert feature.status is FeatureStatus.INSUFFICIENT_DATA
    assert feature.value is None
    assert FeatureQualityFlag.GAP_DETECTED in feature.quality_flags


def test_gap_before_latest_window_is_excluded_but_retained_as_quality_metadata() -> None:
    feature = DerivedFeatureService().calculate(
        _series(_bar(0, close=90), _bar(2, close=100), _bar(3, close=110)),
        feature_request(FeatureType.SIMPLE_RETURN),
    )

    assert feature.status is FeatureStatus.AVAILABLE
    assert feature.value == pytest.approx(0.1)
    assert FeatureQualityFlag.EARLIER_GAP_EXCLUDED in feature.quality_flags
    assert FeatureQualityFlag.GAP_DETECTED not in feature.quality_flags


@pytest.mark.parametrize(
    ("feature_type", "closes", "window"),
    [
        (FeatureType.SIMPLE_RETURN, (0.0, 10.0), None),
        (FeatureType.ROC, (0.0, 5.0, 10.0), 2),
        (FeatureType.LOG_RETURN, (-1.0, 10.0), None),
        (FeatureType.ROLLING_STD, (0.0, 5.0, 10.0), 2),
    ],
)
def test_undefined_ratio_and_log_domains_have_no_value(
    feature_type: FeatureType,
    closes: tuple[float, ...],
    window: int | None,
) -> None:
    feature = DerivedFeatureService().calculate(
        _series(*(_bar(index, close=value) for index, value in enumerate(closes))),
        feature_request(feature_type, window=window),
    )

    assert feature.status is FeatureStatus.UNDEFINED
    assert feature.value is None


def test_flat_inputs_are_valid_and_flagged() -> None:
    feature = DerivedFeatureService().calculate(
        _series(_bar(0, close=100), _bar(1, close=100)),
        feature_request(FeatureType.SIMPLE_RETURN),
    )

    assert feature.status is FeatureStatus.AVAILABLE
    assert feature.value == 0.0
    assert FeatureQualityFlag.FLAT_SERIES in feature.quality_flags


def test_equal_closes_do_not_mark_nonzero_ranges_as_flat() -> None:
    feature = DerivedFeatureService().calculate(
        _series(
            _bar(0, close=100, high=102, low=98),
            _bar(1, close=100, high=103, low=97),
        ),
        feature_request(FeatureType.AVERAGE_RANGE, window=2),
    )

    assert feature.value == pytest.approx(5.0)
    assert FeatureQualityFlag.FLAT_SERIES not in feature.quality_flags


def test_invalid_nonfinite_projected_input_is_rejected_defensively() -> None:
    series = _series(_bar(0, close=100))
    invalid_bar = replace(series.bars[0], close=math.inf)
    invalid_series = replace(series, bars=(invalid_bar,))

    feature = DerivedFeatureService().calculate(
        invalid_series,
        feature_request(FeatureType.BAR_RANGE),
    )

    assert feature.status is FeatureStatus.REJECTED_INPUT
    assert feature.value is None


def test_feature_ids_and_snapshot_are_deterministic_across_insertion_order() -> None:
    bars = (_bar(2, close=102), _bar(0, close=100), _bar(1, close=101))
    first = _series(*bars)
    second = _series(*reversed(bars))
    requests = (
        feature_request(FeatureType.SMA, window=2),
        feature_request(FeatureType.BAR_RANGE),
    )
    service = DerivedFeatureService()

    left = service.calculate_snapshot((first,), requests)
    right = service.calculate_snapshot((second,), tuple(reversed(requests)))

    assert left == right
    assert tuple(feature.feature_id for feature in left.features) == tuple(
        sorted(feature.feature_id for feature in left.features)
    )


def test_feature_identity_changes_with_method_parameters_or_input_lineage() -> None:
    first_series = _series(_bar(0, close=100), _bar(1, close=110))
    revised_series = _series(_bar(0, close=100), _bar(1, close=120))
    service = DerivedFeatureService()

    short = service.calculate(first_series, feature_request(FeatureType.SMA, window=1))
    long = service.calculate(first_series, feature_request(FeatureType.SMA, window=2))
    revised = service.calculate(revised_series, feature_request(FeatureType.SMA, window=1))

    assert len({short.feature_id, long.feature_id, revised.feature_id}) == 3
    assert short.input_series_fingerprint != revised.input_series_fingerprint
    changed_method_version = feature_identity(
        feature_type=short.feature_type,
        parameters=short.parameters,
        method_id=short.method_id,
        method_version="2.0.0",
        input_series_id=short.input_series_id,
        input_series_fingerprint=short.input_series_fingerprint,
        input_first_bar_id=short.input_first_bar_id,
        input_last_bar_id=short.input_last_bar_id,
    )
    assert changed_method_version != short.feature_id


def test_requests_reject_missing_extra_duplicate_or_invalid_window_parameters() -> None:
    series = _series(_bar(0, close=100), _bar(1, close=101))
    service = DerivedFeatureService()
    with pytest.raises(ValueError, match="requires exactly one window"):
        service.calculate(series, DerivedFeatureRequest(feature_type=FeatureType.SMA))
    with pytest.raises(ValueError, match="does not accept parameters"):
        service.calculate(
            series,
            feature_request(FeatureType.BAR_RANGE, window=2),
        )
    with pytest.raises(ValueError, match="positive integer"):
        service.calculate(
            series,
            DerivedFeatureRequest(
                feature_type=FeatureType.SMA,
                parameters=(FeatureParameter(name="window", value=0),),
            ),
        )
    with pytest.raises(ValidationError, match="unique"):
        DerivedFeatureRequest(
            feature_type=FeatureType.SMA,
            parameters=(
                FeatureParameter(name="window", value=2),
                FeatureParameter(name="window", value=3),
            ),
        )


def test_feature_parameters_are_canonically_ordered() -> None:
    request = DerivedFeatureRequest(
        feature_type=FeatureType.SMA,
        parameters=(
            FeatureParameter(name="zeta", value="last"),
            FeatureParameter(name="alpha", value=1),
        ),
    )

    assert tuple(item.name for item in request.parameters) == ("alpha", "zeta")


def test_feature_parameters_reject_nonfinite_and_boolean_values() -> None:
    with pytest.raises(ValidationError, match="finite"):
        FeatureParameter(name="window", value=math.inf)
    with pytest.raises(ValidationError, match="boolean"):
        FeatureParameter(name="window", value=True)


def test_multiple_series_remain_isolated_by_symbol_venue_timeframe_and_source() -> None:
    series = (
        _series(_bar(0, close=100, symbol="XAUUSD", venue="A")),
        _series(_bar(0, close=100, symbol="EURUSD", venue="A")),
        _series(_bar(0, close=100, symbol="XAUUSD", venue="B")),
        _series(_bar(0, close=100, symbol="XAUUSD", venue="A", timeframe="2h")),
        _series(_bar(0, close=100, symbol="XAUUSD", source_id="other")),
    )
    snapshot = DerivedFeatureService().calculate_snapshot(
        series,
        (feature_request(FeatureType.BAR_RANGE),),
    )

    assert len(snapshot.features) == 5
    assert len({feature.input_series_id for feature in snapshot.features}) == 5
    assert {feature.symbol for feature in snapshot.features} == {"XAUUSD", "EURUSD"}
    assert {feature.timeframe for feature in snapshot.features} == {"1h", "2h"}


def test_method_lineage_is_explicit_and_available_values_must_be_finite() -> None:
    series = _series(_bar(0, close=100), _bar(1, close=110), _bar(2, close=99))
    requests = tuple(
        feature_request(feature_type, window=2)
        if feature_type in {
            FeatureType.AVERAGE_RANGE,
            FeatureType.SMA,
            FeatureType.ROC,
            FeatureType.ROLLING_STD,
        }
        else feature_request(feature_type)
        for feature_type in FeatureType
    )
    features = DerivedFeatureService().calculate_snapshot((series,), requests).features

    assert {feature.feature_type: feature.method_id for feature in features} == {
        FeatureType.BAR_RANGE: "friday.bar_range",
        FeatureType.AVERAGE_RANGE: "friday.average_range_arithmetic_mean",
        FeatureType.SIMPLE_RETURN: "friday.simple_close_return",
        FeatureType.LOG_RETURN: "friday.natural_log_close_return",
        FeatureType.SMA: "friday.simple_moving_average",
        FeatureType.ROC: "friday.close_rate_of_change",
        FeatureType.ROLLING_STD: "friday.population_std_simple_returns",
    }
    assert {feature.method_version for feature in features} == {"1.0.0"}
    assert all(
        feature.parameters == (FeatureParameter(name="window", value=2),)
        for feature in features
        if feature.feature_type
        in {
            FeatureType.AVERAGE_RANGE,
            FeatureType.SMA,
            FeatureType.ROC,
            FeatureType.ROLLING_STD,
        }
    )
    available = next(feature for feature in features if feature.status is FeatureStatus.AVAILABLE)
    payload = available.model_dump()
    payload["value"] = math.nan
    with pytest.raises(ValidationError, match="finite"):
        DerivedMarketFeature.model_validate(payload)


def test_router_exposes_only_approved_feature_types_to_chartist_and_oracle() -> None:
    series = _series(_bar(0, close=100), _bar(1, close=110), _bar(2, close=99))
    requests = tuple(
        feature_request(feature_type, window=2)
        if feature_type in {
            FeatureType.AVERAGE_RANGE,
            FeatureType.SMA,
            FeatureType.ROC,
            FeatureType.ROLLING_STD,
        }
        else feature_request(feature_type)
        for feature_type in FeatureType
    )
    snapshot = DerivedFeatureService().calculate_snapshot((series,), requests)
    router = DerivedFeatureRouter()

    for agent_id in NINE_SPECIALIST_IDS:
        assigned = router.route(agent_id=agent_id, snapshot=snapshot)
        assigned_types = {feature.feature_type for feature in assigned}
        if agent_id == "chartist_technician":
            assert assigned_types == {
                FeatureType.BAR_RANGE,
                FeatureType.AVERAGE_RANGE,
                FeatureType.SMA,
                FeatureType.ROC,
            }
        elif agent_id == "oracle_quant":
            assert assigned_types == {
                FeatureType.SIMPLE_RETURN,
                FeatureType.LOG_RETURN,
                FeatureType.ROC,
                FeatureType.ROLLING_STD,
            }
        else:
            assert assigned == ()


def test_orchestrator_calculates_one_shared_feature_snapshot_per_analysis_run() -> None:
    class CountingService(DerivedFeatureService):
        def __init__(self) -> None:
            self.calls = 0

        def calculate_snapshot(self, series, requests):
            self.calls += 1
            return super().calculate_snapshot(series, requests)

    class RecordingRouter(DerivedFeatureRouter):
        def __init__(self) -> None:
            self.snapshot_objects: list[int] = []

        def route(self, *, agent_id, snapshot):
            self.snapshot_objects.append(id(snapshot))
            return super().route(agent_id=agent_id, snapshot=snapshot)

    service = CountingService()
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        derived_feature_service=service,
        derived_feature_router=router,
    )
    core.ingest_market_bar(_bar(0, close=100))
    core.ingest_market_bar(_bar(1, close=110))

    response = core.analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Review technical and quant evidence")
    )

    assert service.calls == 1
    assert len(router.snapshot_objects) == len(NINE_SPECIALIST_IDS)
    assert len(set(router.snapshot_objects)) == 1
    assert response.mode == "deterministic_mock"


def test_derived_snapshot_and_nested_models_are_immutable() -> None:
    snapshot = DerivedFeatureService().calculate_snapshot(
        (_series(_bar(0, close=100)),),
        (feature_request(FeatureType.BAR_RANGE),),
    )

    with pytest.raises(ValidationError):
        snapshot.snapshot_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.features[0].value = 1.0  # type: ignore[misc]


def test_derived_features_are_not_persisted_in_raw_evidence_store() -> None:
    store = EvidenceStore()
    store.ingest_market_bar(_bar(0, close=100))
    raw_snapshot = store.snapshot(symbol="XAUUSD")
    evidence_fingerprint = build_evidence_bundle(raw_snapshot).fingerprint
    feature_snapshot = DerivedFeatureService().calculate_snapshot(
        raw_snapshot.market_series,
        (feature_request(FeatureType.BAR_RANGE),),
    )

    assert len(feature_snapshot.features) == 1
    assert not hasattr(raw_snapshot, "derived_features")
    assert not hasattr(store, "derived_features")
    assert build_evidence_bundle(raw_snapshot).fingerprint == evidence_fingerprint
    with pytest.raises(FrozenInstanceError):
        raw_snapshot.market_series = ()  # type: ignore[misc]


def test_derived_feature_modules_are_provider_neutral_and_side_effect_free() -> None:
    import app.agents.specialists as specialists
    import app.models.derived_features as contracts
    import app.services.derived_feature_routing as routing
    import app.services.derived_features as calculations

    rendered = "\n".join(
        inspect.getsource(module).casefold()
        for module in (contracts, routing, calculations)
    )
    forbidden = (
        "alpha_vantage",
        "tradingview",
        "twelve_data",
        "httpx",
        "mt5",
        "place_order",
    )
    assert not any(value in rendered for value in forbidden)

    specialist_source = inspect.getsource(specialists).casefold()
    forbidden_specialist_calculations = (
        "derivedfeatureservice",
        "feature_request(",
        "math.log(",
        "math.fsum(",
        ".high - ",
        ".close / ",
    )
    assert not any(
        value in specialist_source for value in forbidden_specialist_calculations
    )
