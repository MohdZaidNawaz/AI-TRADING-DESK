"""Phase 5B-7 tests for provider-neutral corporate-action evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.base import AnalysisLifecycle
from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import CorporateActionStore, EvidenceRouter, EvidenceStore
from app.models.corporate_actions import (
    CashDividendTerms,
    CorporateActionInstrumentKey,
    CorporateActionLifecycle,
    CorporateActionSource,
    CorporateActionType,
    NormalizedCorporateAction,
    ShareAdjustmentTerms,
)
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistOperationalState, SpecialistStance
from app.services.corporate_actions import CorporateActionIngestionService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = CorporateActionSource(
    source_id="synthetic-corporate-action-source",
    display_name="Synthetic Corporate Action Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.FUNDAMENTAL_DATA,
)


def _instrument(
    *,
    source_id: str = SOURCE.source_id,
    source_instrument_id: str = "ACME-ORDINARY",
    issuer_id: str = "ACME-ISSUER-001",
    symbol: str | None = "ACME",
    venue: str | None = "NYSE",
) -> CorporateActionInstrumentKey:
    return CorporateActionInstrumentKey(
        source_id=source_id,
        source_instrument_id=source_instrument_id,
        issuer_id=issuer_id,
        symbol=symbol,
        venue=venue,
    )


def _action(
    index: int = 0,
    *,
    source: CorporateActionSource = SOURCE,
    instrument: CorporateActionInstrumentKey | None = None,
    occurrence_id: str | None = None,
    action_type: CorporateActionType = CorporateActionType.CASH_DIVIDEND,
    lifecycle: CorporateActionLifecycle = CorporateActionLifecycle.ANNOUNCED,
    version: int = 1,
    revision_of_action_id: str | None = None,
    amount: str = "0.25",
    numerator: int = 2,
    denominator: int = 1,
    published_at: datetime | None = None,
    effective_at: datetime | None = None,
    received_at: datetime | None = None,
) -> NormalizedCorporateAction:
    actual_instrument = instrument or _instrument(source_id=source.source_id)
    actual_published_at = published_at or MOMENT + timedelta(days=index)
    actual_effective_at = effective_at
    if lifecycle is CorporateActionLifecycle.EFFECTIVE and actual_effective_at is None:
        actual_effective_at = actual_published_at + timedelta(days=1)
    initial = NormalizedCorporateAction(
        source=source,
        instrument=actual_instrument,
        occurrence_id=occurrence_id or f"action-{index}",
        action_type=action_type,
        lifecycle=lifecycle,
        cash_dividend_terms=(
            CashDividendTerms(
                amount=EconomicValue(
                    value=Decimal(amount),
                    unit=EconomicValueUnit.CURRENCY,
                    scale=EconomicValueScale.ONE,
                ),
                currency="USD",
            )
            if action_type is CorporateActionType.CASH_DIVIDEND
            else None
        ),
        share_adjustment_terms=(
            ShareAdjustmentTerms(numerator=numerator, denominator=denominator)
            if action_type is not CorporateActionType.CASH_DIVIDEND
            else None
        ),
        published_at=actual_published_at,
        effective_at=actual_effective_at,
        ex_date=date(2026, 11, 1) + timedelta(days=index),
        record_date=date(2026, 11, 2) + timedelta(days=index),
        payment_date=date(2026, 11, 10) + timedelta(days=index),
        action_version=1,
        received_at=received_at or actual_published_at + timedelta(minutes=1),
    )
    if version == 1:
        return initial
    return NormalizedCorporateAction.model_validate(
        {
            **initial.model_dump(round_trip=True),
            "action_version": version,
            "revision_of_action_id": revision_of_action_id or initial.action_id,
        }
    )


def test_contracts_are_frozen_typed_and_exclude_raw_payloads() -> None:
    action = _action()
    with pytest.raises(ValidationError):
        action.published_at = MOMENT  # type: ignore[misc]
    with pytest.raises(ValidationError):
        action.cash_dividend_terms.amount.value = Decimal("2")  # type: ignore[union-attr,misc]
    with pytest.raises(ValidationError, match="raw_payload"):
        NormalizedCorporateAction.model_validate(
            {**action.model_dump(round_trip=True), "raw_payload": {"unsafe": True}}
        )
    with pytest.raises(ValidationError, match="currency unit"):
        CashDividendTerms(
            amount=EconomicValue(value=Decimal("1"), unit=EconomicValueUnit.COUNT),
            currency="USD",
        )
    with pytest.raises(ValidationError, match="whole numbers"):
        ShareAdjustmentTerms(numerator=True, denominator=1)


def test_time_lifecycle_terms_and_revision_semantics_are_strict() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    effective = _action(
        lifecycle=CorporateActionLifecycle.EFFECTIVE,
        published_at=MOMENT.astimezone(offset),
        effective_at=(MOMENT + timedelta(days=1)).astimezone(offset),
    )
    assert effective.published_at == MOMENT
    assert effective.effective_at == MOMENT + timedelta(days=1)
    assert effective.ex_date == date(2026, 11, 1)
    with pytest.raises(ValidationError, match="require effective_at"):
        NormalizedCorporateAction.model_validate(
            {**effective.model_dump(round_trip=True), "effective_at": None}
        )
    with pytest.raises(ValidationError, match="timezone"):
        _action(published_at=MOMENT.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="cash dividends"):
        NormalizedCorporateAction.model_validate(
            {
                **effective.model_dump(round_trip=True),
                "action_type": CorporateActionType.CASH_DIVIDEND,
                "share_adjustment_terms": ShareAdjustmentTerms(numerator=2, denominator=1),
                "cash_dividend_terms": None,
            }
        )
    original = _action()
    revision = _action(version=2, amount="0.30", revision_of_action_id=original.action_id)
    assert original.action_version == 1
    assert revision.action_version == 2
    with pytest.raises(ValidationError, match="version-one"):
        NormalizedCorporateAction.model_validate(
            {**original.model_dump(round_trip=True), "revision_of_action_id": original.action_id}
        )
    with pytest.raises(ValidationError, match="require revision"):
        NormalizedCorporateAction.model_validate(
            {**original.model_dump(round_trip=True), "action_version": 2}
        )


def test_identity_fingerprint_is_deterministic_and_source_scoped() -> None:
    first = _action(received_at=MOMENT)
    retry = _action(received_at=MOMENT + timedelta(days=5))
    assert first.fingerprint == retry.fingerprint
    assert first.action_id == retry.action_id
    assert _instrument().instrument_id != _instrument(issuer_id="OTHER").instrument_id
    assert _instrument().instrument_id != _instrument(source_instrument_id="ACME-ADR").instrument_id
    assert _action(action_type=CorporateActionType.STOCK_SPLIT).action_id != first.action_id
    assert _action(amount="0.5").action_id != first.action_id
    assert _action(version=2, revision_of_action_id=first.action_id).action_id != first.action_id


def test_store_duplicate_conflict_eviction_and_bounded_metadata() -> None:
    store = CorporateActionStore(
        max_instruments=1,
        max_occurrences_per_instrument=1,
        max_versions_per_occurrence=1,
        max_version_identities=2,
    )
    first = _action(0)
    assert store.ingest(first) is not None
    assert store.ingest(_action(0, received_at=MOMENT + timedelta(days=8))) is not None
    assert store.action_count == 1
    assert store.ingest(_action(1)) is not None
    assert store.action_count == 1
    assert store.ingest(_action(0, received_at=MOMENT + timedelta(days=9))) is None
    with pytest.raises(ValueError, match="conflicting corporate-action"):
        store.ingest(_action(0, amount="9.9"))
    assert store.ingest(_action(2, occurrence_id="other-occurrence")) is not None
    original = _action(3, occurrence_id="revision")
    assert store.ingest(original) is not None
    assert store.ingest(_action(3, occurrence_id="revision", version=2, amount="0.5")) is not None
    other_source = CorporateActionSource(
        source_id="other-action-source",
        display_name="Other Action Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.FUNDAMENTAL_DATA,
    )
    assert store.ingest(
        _action(
            0,
            source=other_source,
            instrument=_instrument(source_id=other_source.source_id),
            amount="9.9",
        )
    ) is not None
    assert store.version_identity_count <= 2


def test_revision_history_is_immutable_and_current_version_is_deterministic() -> None:
    original = _action(occurrence_id="revisable")
    revision = _action(
        occurrence_id="revisable",
        version=2,
        amount="0.30",
        revision_of_action_id=original.action_id,
    )
    store = CorporateActionStore()
    store.ingest(original)
    store.ingest(revision)
    history = store.lifecycle_history(original.instrument, "revisable")
    assert tuple(item.action_version for item in history) == (1, 2)
    assert original.cash_dividend_terms.amount.value == Decimal("0.25")  # type: ignore[union-attr]
    assert history[-1].cash_dividend_terms.amount.value == Decimal("0.30")  # type: ignore[union-attr]
    with pytest.raises(FrozenInstanceError):
        history[0].occurrence_id = "mutated"  # type: ignore[misc]


def test_snapshot_isolation_ordering_and_concurrent_ingestion_are_deterministic() -> None:
    actions = tuple(_action(index) for index in range(8))
    first = CorporateActionStore(max_occurrences_per_instrument=3)
    second = CorporateActionStore(max_occurrences_per_instrument=3)
    for action in actions:
        first.ingest(action)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tuple(pool.map(second.ingest, reversed(actions)))
    assert first.snapshot() == second.snapshot()
    snapshot = first.snapshot(symbol="ACME")
    first.ingest(_action(12))
    assert snapshot != first.snapshot()
    assert len(snapshot.actions) <= 3
    with pytest.raises(FrozenInstanceError):
        snapshot.actions = ()  # type: ignore[misc]


def test_domain_routing_and_provenance_boundaries_are_exact() -> None:
    store = EvidenceStore()
    dividend = _action()
    split = _action(1, action_type=CorporateActionType.STOCK_SPLIT)
    store.ingest_corporate_action(dividend)
    store.ingest_corporate_action(split)
    snapshot = store.snapshot(symbol="ACME")
    router = EvidenceRouter()
    assert {item.action_type for item in router.route(agent_id="athena_analyst", snapshot=snapshot).corporate_actions} == {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.STOCK_SPLIT,
    }
    assert len(router.route(agent_id="scout_recon", snapshot=snapshot).corporate_actions) == 2
    chartist = router.route(agent_id="chartist_technician", snapshot=snapshot)
    assert tuple(item.action_type for item in chartist.corporate_actions) == (CorporateActionType.STOCK_SPLIT,)
    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert ledger.corporate_actions == ()
    assert {item.event_id for item in ledger.provenance} == {dividend.action_id, split.action_id}
    for agent_id in (
        "atlas_macro", "capitol_smart_money", "oracle_quant", "sentinel_risk_officer", "pilot_execution"
    ):
        assert router.route(agent_id=agent_id, snapshot=snapshot).corporate_actions == ()


def test_core_reuses_one_snapshot_and_artifacts_preserve_safety_boundaries() -> None:
    class RecordingRouter(EvidenceRouter):
        def __init__(self) -> None:
            self.snapshot_ids: list[int] = []

        def route(self, *, agent_id: str, snapshot):  # type: ignore[no-untyped-def]
            self.snapshot_ids.append(id(snapshot))
            return super().route(agent_id=agent_id, snapshot=snapshot)

    store = EvidenceStore()
    store.ingest_corporate_action(_action())
    store.ingest_corporate_action(_action(1, action_type=CorporateActionType.STOCK_SPLIT))
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store, evidence_router=router
    )
    snapshot = store.snapshot(symbol="ACME")
    run = core._analyze_run(  # noqa: SLF001 - validates the private snapshot boundary.
        AnalyzeRequest(symbol="ACME", query="fundamental corporate action review"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    assert set(router.snapshot_ids) == {id(snapshot)}
    assert len(artifacts["athena_analyst"].corporate_action_observations) == 2
    assert len(artifacts["scout_recon"].corporate_action_observations) == 2
    assert tuple(item.action_type for item in artifacts["chartist_technician"].corporate_action_observations) == (
        CorporateActionType.STOCK_SPLIT,
    )
    assert artifacts["ledger_the_book"].corporate_action_observations == ()
    assert artifacts["athena_analyst"].stance is SpecialistStance.INSUFFICIENT_DATA
    assert not hasattr(artifacts["athena_analyst"].corporate_action_observations[0], "direction")
    assert artifacts["pilot_execution"].operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert build_evidence_bundle(snapshot).references[-1].event_id in {
        _action().action_id,
        _action(1, action_type=CorporateActionType.STOCK_SPLIT).action_id,
    }


def test_actions_cannot_satisfy_sentinel_or_enable_pilot_and_api_remains_unchanged() -> None:
    store = EvidenceStore()
    store.ingest_corporate_action(_action())
    core = FridayCore(specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store)
    request = AnalyzeRequest(symbol="ACME", query="fundamental corporate action execution")
    response = core.analyze(request)
    workflow = core.run_trade_proposal_workflow(request)
    assert set(response.model_dump()) == {
        "analysis_id", "mode", "symbol", "query", "selected_agents", "routing", "agent_results", "synthesis"
    }
    assert workflow.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    assert workflow.proposal.parameters.direction is None
    assert workflow.proposal.parameters.reference_price is None
    assert not workflow.proposal.observed_market_reference


def test_ingestion_boundary_is_provider_neutral_and_has_no_real_connector() -> None:
    class Consumer:
        def __init__(self) -> None:
            self.store = CorporateActionStore()

        def ingest_corporate_action(self, action: NormalizedCorporateAction):  # type: ignore[no-untyped-def]
            return self.store.ingest(action)

    consumer = Consumer()
    service = CorporateActionIngestionService(consumer=consumer)
    result = service.ingest(_action())
    assert result == consumer.store.snapshot(symbol="ACME").actions[0]
    with pytest.raises(TypeError, match="NormalizedCorporateAction"):
        service.ingest(object())  # type: ignore[arg-type]
