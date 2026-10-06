"""Phase 5B-9 tests for provider-neutral portfolio/account-risk evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.base import AnalysisLifecycle
from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import EvidenceRouter, EvidenceStore, PortfolioRiskStore
from app.models.economic_events import EconomicValue, EconomicValueScale, EconomicValueUnit
from app.models.events import DataDomain, SourceTransport
from app.models.portfolio_risk import (
    AccountStatus,
    NormalizedPortfolioRiskSnapshot,
    PortfolioRiskSource,
    PositionStatus,
    ReportedAccountState,
    ReportedPortfolioPosition,
    ReportedPositionSide,
    ReportedRiskLimit,
    RiskLimitKind,
    SourceAccountKey,
)
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import (
    SpecialistOperationalState,
    SpecialistPortfolioContextState,
    SpecialistStance,
)
from app.services.portfolio_risk import PortfolioRiskIngestionService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = PortfolioRiskSource(
    source_id="synthetic-portfolio-source",
    display_name="Synthetic Portfolio Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.ALTERNATIVE_DATA,
)


def _value(value: str, unit: EconomicValueUnit = EconomicValueUnit.CURRENCY) -> EconomicValue:
    return EconomicValue(value=Decimal(value), unit=unit, scale=EconomicValueScale.ONE)


def _account(*, source_id: str = SOURCE.source_id, account_id: str = "account-001") -> SourceAccountKey:
    return SourceAccountKey(
        source_id=source_id,
        source_account_id=account_id,
        account_reference="Synthetic account",
    )


def _snapshot(
    index: int = 0,
    *,
    source: PortfolioRiskSource = SOURCE,
    account: SourceAccountKey | None = None,
    occurrence: str | None = None,
    version: int = 1,
    revision_of_snapshot_id: str | None = None,
    equity: str = "100000",
    as_of: datetime | None = None,
    received_at: datetime | None = None,
) -> NormalizedPortfolioRiskSnapshot:
    actual_account = account or _account(source_id=source.source_id)
    moment = as_of or MOMENT + timedelta(days=index)
    initial = NormalizedPortfolioRiskSnapshot(
        source=source,
        account=actual_account,
        snapshot_occurrence_id=occurrence or f"snapshot-{index}",
        account_state=ReportedAccountState(
            currency="USD",
            balance=_value("99000"),
            equity=_value(equity),
            available_funds=_value("75000"),
            margin_used=_value("25000"),
            margin_available=_value("75000"),
            unrealized_pnl=_value("1000"),
            realized_pnl=_value("500"),
            status=AccountStatus.ACTIVE,
        ),
        positions=(
            ReportedPortfolioPosition(
                source_position_id="position-001",
                source_instrument_id="XAUUSD-SPOT",
                source_symbol="XAUUSD",
                venue="SYNTHETIC",
                reported_side=ReportedPositionSide.LONG,
                quantity=_value("2", EconomicValueUnit.COUNT),
                price_currency="USD",
                average_entry_price=_value("2500"),
                reference_price=_value("2520"),
                realized_pnl=_value("0"),
                unrealized_pnl=_value("40"),
                pnl_currency="USD",
                status=PositionStatus.OPEN,
                opened_at=moment - timedelta(days=1),
            ),
        ),
        risk_limits=(
            ReportedRiskLimit(
                source_limit_id="daily-loss",
                kind=RiskLimitKind.DAILY_LOSS,
                value=_value("1000"),
                label="Reported daily loss limit",
            ),
        ),
        as_of=moment,
        published_at=moment,
        snapshot_version=1,
        received_at=received_at or moment + timedelta(minutes=1),
    )
    if version == 1:
        return initial
    return NormalizedPortfolioRiskSnapshot.model_validate(
        {
            **initial.model_dump(round_trip=True),
            "snapshot_version": version,
            "revision_of_snapshot_id": revision_of_snapshot_id or initial.snapshot_id,
        }
    )


def test_contracts_are_frozen_utc_and_exclude_payloads_or_calculations() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValidationError):
        snapshot.account_state.equity = _value("1")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.positions[0].quantity.value = Decimal("3")  # type: ignore[misc]
    with pytest.raises(ValidationError, match="raw_payload"):
        NormalizedPortfolioRiskSnapshot.model_validate(
            {**snapshot.model_dump(round_trip=True), "raw_payload": {"unsafe": True}}
        )
    with pytest.raises(ValidationError, match="timezone"):
        _snapshot(as_of=MOMENT.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="currency unit"):
        ReportedAccountState(currency="USD", equity=_value("1", EconomicValueUnit.COUNT))
    with pytest.raises(ValidationError, match="price_currency"):
        ReportedPortfolioPosition(
            source_position_id="bad",
            source_instrument_id="instrument",
            quantity=_value("1", EconomicValueUnit.COUNT),
            average_entry_price=_value("1"),
        )


def test_identity_fingerprint_source_scope_and_revision_validation() -> None:
    first = _snapshot(received_at=MOMENT)
    retry = _snapshot(received_at=MOMENT + timedelta(days=4))
    assert first.fingerprint == retry.fingerprint
    assert first.snapshot_id == retry.snapshot_id
    assert _account(account_id="one").account_id != _account(account_id="two").account_id
    with pytest.raises(ValidationError, match="version-one"):
        NormalizedPortfolioRiskSnapshot.model_validate(
            {**first.model_dump(round_trip=True), "revision_of_snapshot_id": first.snapshot_id}
        )
    with pytest.raises(ValidationError, match="require revision"):
        NormalizedPortfolioRiskSnapshot.model_validate(
            {**first.model_dump(round_trip=True), "snapshot_version": 2}
        )
    other_source = PortfolioRiskSource(
        source_id="other-portfolio-source",
        display_name="Other Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.ALTERNATIVE_DATA,
    )
    assert _snapshot(source=other_source, account=_account(source_id=other_source.source_id)).snapshot_id != first.snapshot_id


def test_store_duplicate_conflict_post_eviction_and_bounded_metadata() -> None:
    store = PortfolioRiskStore(
        max_accounts=1,
        max_occurrences_per_account=1,
        max_versions_per_occurrence=1,
        max_version_identities=2,
    )
    first = _snapshot(0)
    assert store.ingest(first) is not None
    assert store.ingest(_snapshot(0, received_at=MOMENT + timedelta(days=8))) is not None
    assert store.ingest(_snapshot(1)) is not None
    assert store.snapshot_count == 1
    assert store.ingest(_snapshot(0, received_at=MOMENT + timedelta(days=9))) is None
    with pytest.raises(ValueError, match="conflicting portfolio-risk"):
        store.ingest(_snapshot(0, equity="90000"))
    assert store.ingest(_snapshot(2, occurrence="another-occurrence")) is not None
    original = _snapshot(3, occurrence="revision")
    assert store.ingest(original) is not None
    assert store.ingest(_snapshot(3, occurrence="revision", version=2)) is not None
    assert store.version_identity_count <= 2


def test_revision_history_snapshot_ordering_and_concurrency_are_deterministic() -> None:
    original = _snapshot(occurrence="revisable")
    revised = _snapshot(
        occurrence="revisable",
        version=2,
        equity="99000",
        revision_of_snapshot_id=original.snapshot_id,
    )
    store = PortfolioRiskStore(max_occurrences_per_account=3)
    store.ingest(original)
    store.ingest(revised)
    history = store.revision_history(original.account, "revisable")
    assert tuple(item.snapshot_version for item in history) == (1, 2)
    assert history[0].account_state.equity.value == Decimal("100000")  # type: ignore[union-attr]
    with pytest.raises(FrozenInstanceError):
        history[0].snapshot_occurrence_id = "changed"  # type: ignore[misc]

    snapshots = tuple(_snapshot(index) for index in range(8))
    first = PortfolioRiskStore(max_occurrences_per_account=3)
    second = PortfolioRiskStore(max_occurrences_per_account=3)
    for item in snapshots:
        first.ingest(item)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tuple(pool.map(second.ingest, reversed(snapshots)))
    assert first.snapshot() == second.snapshot()


def test_exact_routing_and_ledger_provenance_are_enforced() -> None:
    store = EvidenceStore()
    snapshot = _snapshot()
    store.ingest_portfolio_risk_snapshot(snapshot)
    evidence = store.snapshot(symbol="XAUUSD")
    router = EvidenceRouter()
    sentinel = router.route(agent_id="sentinel_risk_officer", snapshot=evidence)
    assert len(sentinel.portfolio_risk_snapshots) == 1
    ledger = router.route(agent_id="ledger_the_book", snapshot=evidence)
    assert ledger.portfolio_risk_snapshots == ()
    assert {item.event_id for item in ledger.provenance} == {snapshot.snapshot_id}
    for agent_id in (
        "atlas_macro", "capitol_smart_money", "scout_recon", "athena_analyst",
        "chartist_technician", "oracle_quant", "pilot_execution",
    ):
        assert router.route(agent_id=agent_id, snapshot=evidence).portfolio_risk_snapshots == ()


def test_sentinel_artifact_receives_facts_only_and_one_snapshot_is_reused() -> None:
    class RecordingRouter(EvidenceRouter):
        def __init__(self) -> None:
            self.snapshot_ids: list[int] = []

        def route(self, *, agent_id: str, snapshot):  # type: ignore[no-untyped-def]
            self.snapshot_ids.append(id(snapshot))
            return super().route(agent_id=agent_id, snapshot=snapshot)

    store = EvidenceStore()
    store.ingest_portfolio_risk_snapshot(_snapshot())
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store, evidence_router=router
    )
    evidence = store.snapshot(symbol="XAUUSD")
    run = core._analyze_run(  # noqa: SLF001 - verifies the shared immutable snapshot.
        AnalyzeRequest(symbol="XAUUSD", query="risk portfolio review"),
        snapshot=evidence,
        lifecycle=AnalysisLifecycle(),
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    sentinel = artifacts["sentinel_risk_officer"]
    assert set(router.snapshot_ids) == {id(evidence)}
    assert len(sentinel.portfolio_risk_observations) == 1
    assert sentinel.portfolio_context_state is SpecialistPortfolioContextState.PORTFOLIO_CONTEXT_AVAILABLE
    assert sentinel.stance is SpecialistStance.INSUFFICIENT_DATA
    assert not hasattr(sentinel.portfolio_risk_observations[0], "direction")
    assert artifacts["ledger_the_book"].portfolio_risk_observations == ()
    assert artifacts["pilot_execution"].operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert any(item.event_id == _snapshot().snapshot_id for item in build_evidence_bundle(evidence).references)


def test_portfolio_evidence_satisfies_only_portfolio_context_and_api_is_unchanged() -> None:
    store = EvidenceStore()
    store.ingest_portfolio_risk_snapshot(_snapshot())
    core = FridayCore(specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store)
    request = AnalyzeRequest(symbol="XAUUSD", query="risk portfolio execution review")
    response = core.analyze(request)
    workflow = core.run_trade_proposal_workflow(request)
    assert set(response.model_dump()) == {
        "analysis_id", "mode", "symbol", "query", "selected_agents", "routing", "agent_results", "synthesis"
    }
    assert workflow.sentinel_decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert "portfolio_context" not in workflow.sentinel_decision.missing_requirements
    assert "market_evidence" in workflow.sentinel_decision.missing_requirements
    assert "risk_context" in workflow.sentinel_decision.missing_requirements
    assert workflow.proposal.parameters.direction is None
    assert workflow.proposal.parameters.reference_price is None


def test_provider_neutral_ingestion_service_has_no_connector_or_network() -> None:
    store = EvidenceStore()

    class Consumer:
        def ingest_portfolio_risk_snapshot(self, snapshot: NormalizedPortfolioRiskSnapshot) -> object:
            return store.ingest_portfolio_risk_snapshot(snapshot)

    service = PortfolioRiskIngestionService(consumer=Consumer())
    result = service.ingest(_snapshot())
    assert result == store.snapshot().portfolio_risk_snapshots[0]
    with pytest.raises(TypeError, match="NormalizedPortfolioRiskSnapshot"):
        service.ingest(object())  # type: ignore[arg-type]
