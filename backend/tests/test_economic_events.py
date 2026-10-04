"""Phase 5B-2 tests for provider-neutral economic-event evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.master import MasterAgent
from app.agents.base import AnalysisLifecycle
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import EvidenceRouter, EvidenceStore
from app.evidence.economic_store import EconomicEventStore
from app.models.economic_events import (
    EconomicEventSource,
    EconomicImpact,
    EconomicReleaseStatus,
    EconomicValue,
    EconomicValueScale,
    EconomicValueUnit,
    NormalizedEconomicEvent,
)
from app.models.events import DataDomain, SourceTransport
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import (
    SpecialistOperationalState,
    SpecialistStance,
)
from app.services.economic_events import EconomicEventIngestionService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOURCE = EconomicEventSource(
    source_id="economic-test-source",
    display_name="Economic Test Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.ECONOMIC_DATA,
)


def _value(
    value: str,
    *,
    unit: EconomicValueUnit = EconomicValueUnit.PERCENT,
    scale: EconomicValueScale = EconomicValueScale.ONE,
) -> EconomicValue:
    return EconomicValue(value=Decimal(value), unit=unit, scale=scale)


def _event(
    *,
    occurrence_id: str = "us-cpi-2026-08",
    event_name: str = "US Consumer Price Index",
    source: EconomicEventSource = SOURCE,
    scheduled_at: datetime = MOMENT,
    released_at: datetime | None = MOMENT + timedelta(seconds=2),
    release_status: EconomicReleaseStatus = EconomicReleaseStatus.RELEASED,
    expected_impact: EconomicImpact = EconomicImpact.HIGH,
    previous: EconomicValue | None = None,
    revised_previous: EconomicValue | None = None,
    forecast: EconomicValue | None = None,
    actual: EconomicValue | None = None,
    release_version: int = 1,
    revision_of_event_id: str | None = None,
    received_at: datetime = MOMENT + timedelta(seconds=3),
    country: str | None = "US",
    region: str | None = "North America",
    currency: str | None = "USD",
) -> NormalizedEconomicEvent:
    return NormalizedEconomicEvent(
        source=source,
        occurrence_id=occurrence_id,
        event_name=event_name,
        indicator_id="US-CPI-ALL",
        country=country,
        region=region,
        currency=currency,
        reference_period="2026-08",
        scheduled_at=scheduled_at,
        released_at=released_at,
        release_status=release_status,
        expected_impact=expected_impact,
        previous=previous,
        revised_previous=revised_previous,
        forecast=forecast,
        actual=actual,
        release_version=release_version,
        revision_of_event_id=revision_of_event_id,
        received_at=received_at,
    )


def _scheduled(**overrides: object) -> NormalizedEconomicEvent:
    values: dict[str, object] = {
        "released_at": None,
        "release_status": EconomicReleaseStatus.SCHEDULED,
        "actual": None,
    }
    values.update(overrides)
    return _event(**values)  # type: ignore[arg-type]


def test_economic_values_are_frozen_finite_and_preserve_units_and_scales() -> None:
    value = _value(
        "1250.50",
        unit=EconomicValueUnit.CURRENCY,
        scale=EconomicValueScale.MILLION,
    )

    assert value.value == Decimal("1250.50")
    assert value.unit is EconomicValueUnit.CURRENCY
    assert value.scale is EconomicValueScale.MILLION
    with pytest.raises(ValidationError):
        value.value = Decimal("1")  # type: ignore[misc]
    for invalid in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValidationError, match="finite"):
            _value(invalid)


def test_all_economic_value_units_and_scales_are_typed() -> None:
    assert {item.name for item in EconomicValueUnit} == {
        "PERCENT",
        "PERCENTAGE_POINT",
        "BASIS_POINT",
        "INDEX_POINT",
        "COUNT",
        "CURRENCY",
        "RATE",
        "LEVEL",
        "UNKNOWN",
    }
    assert {item.name for item in EconomicValueScale} == {
        "ONE",
        "THOUSAND",
        "MILLION",
        "BILLION",
        "TRILLION",
    }
    unknown = _value("7", unit=EconomicValueUnit.UNKNOWN)
    assert unknown.unit is EconomicValueUnit.UNKNOWN
    assert unknown.value == Decimal("7")


def test_normalized_event_is_frozen_requires_geography_and_forbids_raw_payload() -> None:
    event = _event()
    with pytest.raises(ValidationError):
        event.event_name = "Changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="country, region, or currency"):
        _event(country=None, region=None, currency=None)
    with pytest.raises(ValidationError):
        NormalizedEconomicEvent(
            **_event().model_dump(exclude={"event_id", "fingerprint"}),
            raw_payload={"provider": "forbidden"},
        )


def test_required_and_optional_economic_event_fields_are_enforced() -> None:
    payload = _event().model_dump(exclude={"event_id", "fingerprint"})
    for required_field in (
        "source",
        "occurrence_id",
        "event_name",
        "scheduled_at",
        "release_status",
        "release_version",
        "received_at",
    ):
        incomplete = dict(payload)
        incomplete.pop(required_field)
        with pytest.raises(ValidationError):
            NormalizedEconomicEvent(**incomplete)

    minimal = NormalizedEconomicEvent(
        source=SOURCE,
        occurrence_id="minimal-event",
        event_name="Qualitative policy event",
        country="US",
        scheduled_at=MOMENT,
        released_at=MOMENT,
        release_status=EconomicReleaseStatus.RELEASED,
        release_version=1,
        received_at=MOMENT,
    )
    assert minimal.indicator_id is None
    assert minimal.region is None
    assert minimal.currency is None
    assert minimal.reference_period is None
    assert minimal.previous is None
    assert minimal.revised_previous is None
    assert minimal.forecast is None
    assert minimal.actual is None


def test_timestamp_normalization_reference_period_and_semantic_value_separation() -> None:
    plus_five_thirty = timezone(timedelta(hours=5, minutes=30))
    event = _event(
        scheduled_at=MOMENT.astimezone(plus_five_thirty),
        released_at=(MOMENT + timedelta(seconds=2)).astimezone(plus_five_thirty),
        received_at=(MOMENT + timedelta(seconds=3)).astimezone(plus_five_thirty),
        previous=_value("3.0"),
        revised_previous=_value("3.1"),
        forecast=_value("3.2"),
        actual=_value("3.4"),
    )

    assert event.scheduled_at == MOMENT
    assert event.released_at == MOMENT + timedelta(seconds=2)
    assert event.received_at == MOMENT + timedelta(seconds=3)
    assert event.reference_period == "2026-08"
    assert event.previous != event.revised_previous
    assert event.forecast != event.actual
    for field in ("scheduled_at", "released_at", "received_at"):
        with pytest.raises(ValidationError, match="timezone"):
            _event(**{field: datetime(2026, 9, 30, 12, 0)})


@pytest.mark.parametrize(
    ("status", "released_at", "actual", "version", "revision_id"),
    (
        (EconomicReleaseStatus.SCHEDULED, None, None, 1, None),
        (EconomicReleaseStatus.RELEASED, MOMENT, None, 1, None),
        (EconomicReleaseStatus.REVISED, MOMENT, _value("3.4"), 2, "earlier-event"),
        (EconomicReleaseStatus.DELAYED, None, None, 1, None),
        (EconomicReleaseStatus.CANCELLED, None, None, 1, None),
        (EconomicReleaseStatus.UNKNOWN, None, None, 1, None),
    ),
)
def test_all_six_lifecycle_states_are_supported(
    status: EconomicReleaseStatus,
    released_at: datetime | None,
    actual: EconomicValue | None,
    version: int,
    revision_id: str | None,
) -> None:
    event = _event(
        release_status=status,
        released_at=released_at,
        actual=actual,
        release_version=version,
        revision_of_event_id=revision_id,
    )
    assert event.release_status is status


def test_invalid_lifecycle_combinations_are_rejected() -> None:
    with pytest.raises(ValidationError, match="scheduled events"):
        _event(release_status=EconomicReleaseStatus.SCHEDULED, actual=_value("1"))
    with pytest.raises(ValidationError, match="require released_at"):
        _event(release_status=EconomicReleaseStatus.RELEASED, released_at=None)
    with pytest.raises(ValidationError, match="cannot contain release data"):
        _event(release_status=EconomicReleaseStatus.DELAYED, actual=_value("1"))
    with pytest.raises(ValidationError, match="cannot contain release data"):
        _event(release_status=EconomicReleaseStatus.CANCELLED, released_at=MOMENT)
    with pytest.raises(ValidationError, match="greater than one"):
        _event(
            release_status=EconomicReleaseStatus.REVISED,
            revision_of_event_id="earlier",
        )
    with pytest.raises(ValidationError, match="revision_of_event_id"):
        _event(
            release_status=EconomicReleaseStatus.REVISED,
            release_version=2,
        )
    with pytest.raises(ValidationError, match="reserved"):
        _event(revision_of_event_id="unexpected")


def test_unknown_rejects_release_specific_data_but_valid_unknown_is_allowed() -> None:
    with pytest.raises(ValidationError, match="unknown events cannot contain release data"):
        _event(
            release_status=EconomicReleaseStatus.UNKNOWN,
            released_at=MOMENT,
            actual=None,
        )
    with pytest.raises(ValidationError, match="unknown events cannot contain release data"):
        _event(
            release_status=EconomicReleaseStatus.UNKNOWN,
            released_at=None,
            actual=_value("3.4"),
        )

    event = _event(
        release_status=EconomicReleaseStatus.UNKNOWN,
        released_at=None,
        actual=None,
    )
    assert event.release_status is EconomicReleaseStatus.UNKNOWN
    assert event.released_at is None
    assert event.actual is None


def test_qualitative_release_may_omit_actual() -> None:
    event = _event(
        event_name="Federal Reserve policy statement",
        actual=None,
        forecast=None,
    )
    assert event.release_status is EconomicReleaseStatus.RELEASED
    assert event.actual is None


def test_ids_and_fingerprints_are_deterministic_and_ignore_receipt_noise() -> None:
    first = _event(received_at=MOMENT + timedelta(seconds=3))
    second = _event(received_at=MOMENT + timedelta(minutes=2))
    changed = _event(event_name="US Core Consumer Price Index")

    assert first.fingerprint == second.fingerprint
    assert first.event_id == second.event_id
    assert changed.fingerprint != first.fingerprint
    assert changed.event_id != first.event_id


def test_store_suppresses_duplicates_and_rejects_same_version_conflicts() -> None:
    store = EconomicEventStore()
    later_receipt = _event(received_at=MOMENT + timedelta(minutes=1))
    earlier_receipt = _event(received_at=MOMENT + timedelta(seconds=3))

    store.ingest(later_receipt)
    store.ingest(earlier_receipt)
    assert store.event_count == 1
    assert store.snapshot().events[0].received_at == earlier_receipt.received_at

    with pytest.raises(ValueError, match="conflicting economic-event content"):
        store.ingest(_event(actual=_value("9.9")))


def test_same_version_conflict_is_rejected_after_full_event_eviction() -> None:
    store = EconomicEventStore(max_events=1)
    original = _event(occurrence_id="evicted-occurrence")
    retained = _scheduled(
        occurrence_id="retained-occurrence",
        scheduled_at=MOMENT + timedelta(days=1),
        received_at=MOMENT + timedelta(days=1),
    )
    store.ingest(original)
    store.ingest(retained)
    assert tuple(item.occurrence_id for item in store.snapshot().events) == (
        "retained-occurrence",
    )

    with pytest.raises(ValueError, match="conflicting economic-event content"):
        store.ingest(
            _event(
                occurrence_id=original.occurrence_id,
                event_name="Conflicting normalized content",
            )
        )


def test_exact_duplicate_after_full_event_eviction_remains_non_conflicting() -> None:
    store = EconomicEventStore(max_events=1)
    original = _event(occurrence_id="evicted-duplicate")
    store.ingest(original)
    store.ingest(
        _scheduled(
            occurrence_id="newer-retained-event",
            scheduled_at=MOMENT + timedelta(days=1),
            received_at=MOMENT + timedelta(days=1),
        )
    )

    duplicate = _event(
        occurrence_id=original.occurrence_id,
        received_at=MOMENT + timedelta(hours=2),
    )
    store.ingest(duplicate)

    assert store.event_count == 1
    assert store.version_identity_count == 2
    assert store.snapshot().events[0].occurrence_id == "newer-retained-event"


def test_version_conflict_identity_isolated_by_occurrence_version_and_source() -> None:
    second_source = EconomicEventSource(
        source_id="independent-economic-source",
        display_name="Independent Economic Source",
        transport=SourceTransport.FEED,
        domain=DataDomain.ECONOMIC_DATA,
    )
    store = EconomicEventStore(max_events=1)
    original = _event(occurrence_id="identity-boundary")
    store.ingest(original)
    store.ingest(
        _scheduled(
            occurrence_id="eviction-trigger",
            scheduled_at=MOMENT + timedelta(days=1),
            received_at=MOMENT + timedelta(days=1),
        )
    )

    store.ingest(_event(occurrence_id="different-occurrence", event_name="Different content"))
    store.ingest(
        _event(
            occurrence_id=original.occurrence_id,
            release_version=2,
            event_name="Different version content",
        )
    )
    store.ingest(
        _event(
            source=second_source,
            occurrence_id=original.occurrence_id,
            event_name="Different provider content",
        )
    )
    assert store.event_count == 1


def test_conflict_metadata_is_bounded_and_deterministic() -> None:
    events = tuple(
        _scheduled(
            occurrence_id=f"bounded-identity-{index}",
            scheduled_at=MOMENT + timedelta(hours=index),
            received_at=MOMENT + timedelta(hours=index),
        )
        for index in range(8)
    )
    first = EconomicEventStore(max_events=1, max_version_identities=3)
    second = EconomicEventStore(max_events=1, max_version_identities=3)
    for event in events:
        first.ingest(event)
    for event in reversed(events):
        second.ingest(event)

    assert first.version_identity_count == 3
    assert second.version_identity_count == 3
    assert first._version_fingerprints == second._version_fingerprints  # noqa: SLF001
    assert first.snapshot() == second.snapshot()


def test_conflict_metadata_capacity_cannot_be_smaller_than_event_capacity() -> None:
    with pytest.raises(ValueError, match="cannot be less than max_events"):
        EconomicEventStore(max_events=2, max_version_identities=1)


def test_multiple_known_fingerprints_for_one_version_are_treated_as_conflict() -> None:
    store = EconomicEventStore()
    event = _event(occurrence_id="corrupt-version-identity")
    store.ingest(event)
    version_key = (SOURCE.source_id, event.occurrence_id, event.release_version)
    store._version_fingerprints[version_key] = frozenset(  # noqa: SLF001
        {event.fingerprint, "f" * 64}
    )

    with pytest.raises(ValueError, match="conflicting economic-event content"):
        store.ingest(event)


def test_out_of_order_insertion_has_identical_snapshot_and_latest_version() -> None:
    scheduled = _scheduled()
    released = _event(release_version=2, actual=_value("3.2"))
    revised = _event(
        release_status=EconomicReleaseStatus.REVISED,
        release_version=3,
        revision_of_event_id=released.event_id,
        actual=_value("3.3"),
        revised_previous=_value("3.0"),
    )
    first = EconomicEventStore()
    second = EconomicEventStore()
    for event in (scheduled, released, revised):
        first.ingest(event)
    for event in (revised, released, scheduled):
        second.ingest(event)

    assert first.snapshot() == second.snapshot()
    assert first.snapshot().fingerprint == second.snapshot().fingerprint
    assert first.latest_versions() == (first.snapshot().events[-1],)
    assert tuple(item.release_version for item in first.lifecycle_history(scheduled.occurrence_id)) == (
        1,
        2,
        3,
    )


def test_store_is_bounded_and_retention_is_insertion_order_independent() -> None:
    events = tuple(
        _scheduled(
            occurrence_id=f"event-{index}",
            scheduled_at=MOMENT + timedelta(hours=index),
            received_at=MOMENT,
        )
        for index in range(4)
    )
    first = EconomicEventStore(max_events=2)
    second = EconomicEventStore(max_events=2)
    for event in events:
        first.ingest(event)
    for event in reversed(events):
        second.ingest(event)

    assert first.event_count == 2
    assert first.snapshot() == second.snapshot()
    assert tuple(item.occurrence_id for item in first.snapshot().events) == (
        "event-2",
        "event-3",
    )


def test_store_filters_and_provider_isolation() -> None:
    second_source = EconomicEventSource(
        source_id="second-economic-source",
        display_name="Second Economic Source",
        transport=SourceTransport.FEED,
        domain=DataDomain.ECONOMIC_DATA,
    )
    store = EconomicEventStore()
    us_event = _event()
    eu_event = _scheduled(
        occurrence_id="ecb-policy-2026-09",
        source=second_source,
        country="DE",
        region="Europe",
        currency="EUR",
    )
    same_occurrence_other_provider = _event(source=second_source)
    for event in (us_event, eu_event, same_occurrence_other_provider):
        store.ingest(event)

    assert len(store.snapshot(currency="usd").events) == 2
    assert store.snapshot(country="de").events == (store.snapshot(currency="EUR").events[0],)
    assert store.snapshot(statuses={EconomicReleaseStatus.SCHEDULED}).events[0].event_id == eu_event.event_id
    assert len(
        store.snapshot(
            starts_at=MOMENT,
            ends_before=MOMENT + timedelta(seconds=1),
        ).events
    ) == 3
    assert len(store.lifecycle_history(us_event.occurrence_id)) == 2
    assert len(
        store.lifecycle_history(us_event.occurrence_id, source_id=SOURCE.source_id)
    ) == 1
    with pytest.raises(ValueError, match="starts_at"):
        store.snapshot(starts_at=MOMENT + timedelta(days=1), ends_before=MOMENT)


def test_store_is_thread_safe_and_snapshot_collections_are_immutable() -> None:
    store = EconomicEventStore(max_events=100)
    events = tuple(
        _scheduled(occurrence_id=f"thread-{index}", scheduled_at=MOMENT + timedelta(minutes=index))
        for index in range(40)
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(store.ingest, events))

    snapshot = store.snapshot()
    assert store.event_count == 40
    with pytest.raises(FrozenInstanceError):
        snapshot.fingerprint = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.events[0].event_name = "changed"  # type: ignore[misc]


def test_economic_evidence_is_separate_and_routes_only_to_approved_specialists() -> None:
    store = EvidenceStore()
    events = (
        _scheduled(occurrence_id="scheduled"),
        _event(occurrence_id="released"),
        _event(
            occurrence_id="revised",
            release_status=EconomicReleaseStatus.REVISED,
            release_version=2,
            revision_of_event_id="prior-revised-version",
            actual=_value("3.4"),
        ),
        _scheduled(occurrence_id="delayed", release_status=EconomicReleaseStatus.DELAYED),
        _scheduled(occurrence_id="cancelled", release_status=EconomicReleaseStatus.CANCELLED),
        _scheduled(occurrence_id="unknown", release_status=EconomicReleaseStatus.UNKNOWN),
    )
    for event in events:
        store.ingest_economic_event(event)
    snapshot = store.snapshot(symbol="XAUUSD")
    router = EvidenceRouter()

    assert snapshot.market_evidence == ()
    assert len(snapshot.economic_events) == 6
    assert {item.release_status for item in router.route(agent_id="atlas_macro", snapshot=snapshot).economic_events} == {
        EconomicReleaseStatus.SCHEDULED,
        EconomicReleaseStatus.RELEASED,
        EconomicReleaseStatus.REVISED,
    }
    assert {item.release_status for item in router.route(agent_id="scout_recon", snapshot=snapshot).economic_events} == {
        EconomicReleaseStatus.SCHEDULED,
        EconomicReleaseStatus.RELEASED,
        EconomicReleaseStatus.REVISED,
        EconomicReleaseStatus.DELAYED,
        EconomicReleaseStatus.CANCELLED,
    }
    sentinel_events = router.route(
        agent_id="sentinel_risk_officer",
        snapshot=snapshot,
    ).economic_events
    assert len(sentinel_events) == 6
    assert all(item.expected_impact is EconomicImpact.HIGH for item in sentinel_events)
    assert all(item.provenance.source_id == SOURCE.source_id for item in sentinel_events)
    assert all(
        item.previous is None
        and item.revised_previous is None
        and item.forecast is None
        and item.actual is None
        for item in sentinel_events
    )
    assert len(router.route(agent_id="ledger_the_book", snapshot=snapshot).economic_events) == 6
    for agent_id in (
        "capitol_smart_money",
        "athena_analyst",
        "chartist_technician",
        "oracle_quant",
        "pilot_execution",
    ):
        assert router.route(agent_id=agent_id, snapshot=snapshot).economic_events == ()


def test_router_uses_latest_version_for_current_specialists_and_full_history_for_ledger() -> None:
    store = EvidenceStore()
    scheduled = _scheduled()
    released = _event(release_version=2, forecast=_value("3.2"), actual=_value("3.4"))
    store.ingest_economic_event(released)
    store.ingest_economic_event(scheduled)
    snapshot = store.snapshot()
    router = EvidenceRouter()

    assert tuple(
        item.release_version
        for item in router.route(agent_id="atlas_macro", snapshot=snapshot).economic_events
    ) == (2,)
    assert tuple(
        item.release_version
        for item in router.route(agent_id="ledger_the_book", snapshot=snapshot).economic_events
    ) == (1, 2)


class _RecordingRouter(EvidenceRouter):
    def __init__(self) -> None:
        self.snapshot_ids: list[int] = []

    def route(self, *, agent_id: str, snapshot: object):  # type: ignore[override]
        self.snapshot_ids.append(id(snapshot))
        return super().route(agent_id=agent_id, snapshot=snapshot)  # type: ignore[arg-type]


class _CountingStore(EvidenceStore):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_calls = 0

    def snapshot(self, *, symbol: str | None = None):  # type: ignore[override]
        self.snapshot_calls += 1
        return super().snapshot(symbol=symbol)


def test_one_economic_snapshot_is_reused_for_every_specialist_in_an_analysis() -> None:
    store = _CountingStore()
    store.ingest_economic_event(_scheduled())
    router = _RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
        evidence_router=router,
    )

    core.analyze(AnalyzeRequest(symbol="XAUUSD", query="Give a complete desk overview"))

    assert store.snapshot_calls == 1
    assert len(router.snapshot_ids) == 9
    assert len(set(router.snapshot_ids)) == 1


def test_specialist_artifacts_preserve_typed_values_lineage_and_safe_stances() -> None:
    store = EvidenceStore()
    scheduled = _scheduled(
        previous=_value("3.0"),
        forecast=_value("3.2"),
    )
    released = _event(
        release_version=2,
        previous=_value("3.0"),
        revised_previous=_value("3.1"),
        forecast=_value("3.2"),
        actual=_value("3.4"),
    )
    revised = _event(
        release_status=EconomicReleaseStatus.REVISED,
        release_version=3,
        revision_of_event_id=released.event_id,
        previous=_value("3.0"),
        revised_previous=_value("3.1"),
        forecast=_value("3.2"),
        actual=_value("3.3"),
    )
    for event in (scheduled, released, revised):
        store.ingest_economic_event(event)
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    snapshot = store.snapshot(symbol="XAUUSD")
    run = core._analyze_run(  # noqa: SLF001 - verifies internal structured artifacts.
        AnalyzeRequest(symbol="XAUUSD", query="Give a complete desk overview"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )

    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    atlas = artifacts["atlas_macro"]
    ledger = artifacts["ledger_the_book"]
    sentinel = artifacts["sentinel_risk_officer"]
    assert len(atlas.economic_observations) == 1
    observation = atlas.economic_observations[0]
    assert observation.forecast == _value("3.2")
    assert observation.actual == _value("3.3")
    assert observation.previous == _value("3.0")
    assert observation.revised_previous == _value("3.1")
    assert observation.revision_of_event_id == released.event_id
    assert observation.evidence_reference.source_id == SOURCE.source_id
    assert len(ledger.economic_observations) == 3
    assert sentinel.economic_observations[0].actual is None
    assert all(item.stance is SpecialistStance.INSUFFICIENT_DATA for item in artifacts.values())
    assert not hasattr(observation, "direction")
    assert not hasattr(observation, "surprise")
    assert run.master_synthesis.specialist_analyses == tuple(
        sorted(run.specialist_analyses, key=lambda item: (item.agent_id, item.analysis_id))
    )
    assert artifacts["pilot_execution"].operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert artifacts["pilot_execution"].economic_observations == ()


def test_economic_events_do_not_satisfy_market_evidence_or_change_public_api_schema() -> None:
    store = EvidenceStore()
    store.ingest_economic_event(_scheduled())
    core = FridayCore(
        specialists=create_specialist_agents(),
        master=MasterAgent(),
        evidence_store=store,
    )
    response = core.analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Give a complete desk overview")
    )
    workflow = core.run_trade_proposal_workflow(
        AnalyzeRequest(symbol="XAUUSD", query="Give a complete desk overview")
    )

    assert set(response.model_dump()) == {
        "analysis_id",
        "mode",
        "symbol",
        "query",
        "selected_agents",
        "routing",
        "agent_results",
        "synthesis",
    }
    assert workflow.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    assert workflow.proposal.parameters.direction is None
    assert workflow.proposal.parameters.reference_price is None
    assert workflow.proposal.parameters.stop_loss is None
    assert workflow.proposal.parameters.targets == ()


def test_empty_economic_evidence_remains_backward_compatible() -> None:
    snapshot = EvidenceStore().snapshot(symbol="XAUUSD")
    assert snapshot.economic_events == ()
    assert EconomicEventStore().snapshot().fingerprint == (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    for agent_id in (
        "atlas_macro",
        "scout_recon",
        "sentinel_risk_officer",
        "ledger_the_book",
    ):
        assert EvidenceRouter().route(agent_id=agent_id, snapshot=snapshot).economic_events == ()


def test_internal_ingestion_service_uses_the_provider_neutral_consumer_boundary() -> None:
    store = EvidenceStore()

    class Consumer:
        def ingest_economic_event(self, event: NormalizedEconomicEvent) -> object:
            return store.ingest_economic_event(event)

    service = EconomicEventIngestionService(consumer=Consumer())
    result = service.ingest(_scheduled())

    assert result == store.snapshot().economic_events[0]
    with pytest.raises(TypeError, match="NormalizedEconomicEvent"):
        service.ingest(object())  # type: ignore[arg-type]


def test_proposal_traceability_includes_economics_without_treating_it_as_market_data() -> None:
    store = EvidenceStore()
    event = _scheduled(received_at=MOMENT - timedelta(minutes=1))
    store.ingest_economic_event(event)
    snapshot = store.snapshot(symbol="XAUUSD")
    bundle = build_evidence_bundle(snapshot)

    assert len(bundle.references) == 1
    assert bundle.references[0].domain is DataDomain.ECONOMIC_DATA
    assert bundle.references[0].event_id == event.event_id
    assert bundle.references[0].symbol is None
    assert bundle.created_at == event.received_at
    assert len(bundle.fingerprint) == 64
    assert snapshot.market_evidence == ()
