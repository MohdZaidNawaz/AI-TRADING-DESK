"""Phase 5B-8 tests for provider-neutral issuer-reference evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agents.base import AnalysisLifecycle
from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore
from app.agents.specialists import create_specialist_agents
from app.evidence import EvidenceRouter, EvidenceStore, IssuerReferenceStore
from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.issuer_reference import (
    InstrumentKind,
    IssuerClassification,
    IssuerInstrumentReferenceKey,
    IssuerReferenceSource,
    IssuerReferenceStatus,
    NormalizedIssuerInstrumentReference,
)
from app.models.proposals import SentinelDecisionStatus
from app.models.schemas import AnalyzeRequest
from app.models.specialist_analysis import SpecialistOperationalState, SpecialistStance
from app.services.issuer_reference import IssuerReferenceIngestionService
from app.services.trade_proposal_workflow import build_evidence_bundle


MOMENT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SOURCE = IssuerReferenceSource(
    source_id="synthetic-reference-source",
    display_name="Synthetic Reference Source",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.FUNDAMENTAL_DATA,
)


def _instrument(
    *,
    source_id: str = SOURCE.source_id,
    source_issuer_id: str = "ACME-ISSUER-001",
    source_instrument_id: str = "ACME-ORDINARY",
    source_symbol: str | None = "ACME",
    venue: str | None = "NYSE",
) -> IssuerInstrumentReferenceKey:
    return IssuerInstrumentReferenceKey(
        source_id=source_id,
        source_issuer_id=source_issuer_id,
        source_instrument_id=source_instrument_id,
        source_symbol=source_symbol,
        venue=venue,
    )


def _profile(
    index: int = 0,
    *,
    source: IssuerReferenceSource = SOURCE,
    instrument: IssuerInstrumentReferenceKey | None = None,
    source_profile_id: str | None = None,
    version: int = 1,
    revision_of_profile_id: str | None = None,
    issuer_display_name: str = "Acme Holdings",
    as_of: datetime | None = None,
    received_at: datetime | None = None,
) -> NormalizedIssuerInstrumentReference:
    actual_instrument = instrument or _instrument(source_id=source.source_id)
    moment = as_of or MOMENT + timedelta(days=index)
    initial = NormalizedIssuerInstrumentReference(
        source=source,
        instrument=actual_instrument,
        source_profile_id=source_profile_id or f"profile-{index}",
        issuer_display_name=issuer_display_name,
        issuer_legal_name="Acme Holdings Incorporated",
        instrument_display_name="Acme Holdings Common Stock",
        asset_class=AssetClass.EQUITY,
        instrument_kind=InstrumentKind.COMMON_EQUITY,
        status=IssuerReferenceStatus.ACTIVE,
        classification=IssuerClassification(
            taxonomy_id="synthetic-gics",
            taxonomy_name="Synthetic GICS",
            sector="Industrials",
            industry="Conglomerates",
        ),
        country="US",
        domicile_country="US",
        reference_currency="USD",
        as_of=moment,
        published_at=moment,
        profile_version=1,
        received_at=received_at or moment + timedelta(minutes=1),
    )
    if version == 1:
        return initial
    return NormalizedIssuerInstrumentReference.model_validate(
        {
            **initial.model_dump(round_trip=True),
            "profile_version": version,
            "revision_of_profile_id": revision_of_profile_id or initial.profile_id,
        }
    )


def test_contracts_are_frozen_nested_and_reject_raw_or_float_fields() -> None:
    profile = _profile()
    with pytest.raises(ValidationError):
        profile.issuer_display_name = "Changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        profile.classification.sector = "Changed"  # type: ignore[union-attr,misc]
    with pytest.raises(ValidationError, match="raw_payload"):
        NormalizedIssuerInstrumentReference.model_validate(
            {**profile.model_dump(round_trip=True), "raw_payload": {"unsafe": True}}
        )
    with pytest.raises(ValidationError):
        NormalizedIssuerInstrumentReference.model_validate(
            {**profile.model_dump(round_trip=True), "confidence": 0.9}
        )


def test_source_key_timestamp_and_revision_validation_are_strict() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    profile = _profile(as_of=MOMENT.astimezone(offset))
    assert profile.as_of == MOMENT
    with pytest.raises(ValidationError, match="timezone"):
        _profile(as_of=MOMENT.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="version-one"):
        NormalizedIssuerInstrumentReference.model_validate(
            {**profile.model_dump(round_trip=True), "revision_of_profile_id": profile.profile_id}
        )
    with pytest.raises(ValidationError, match="require revision"):
        NormalizedIssuerInstrumentReference.model_validate(
            {**profile.model_dump(round_trip=True), "profile_version": 2}
        )
    with pytest.raises(ValidationError, match="source_id"):
        _profile(instrument=_instrument(source_id="other-source"))
    with pytest.raises(ValidationError, match="fundamental_data"):
        IssuerReferenceSource(
            source_id="invalid-domain",
            display_name="Invalid",
            transport=SourceTransport.MANUAL,
            domain=DataDomain.MARKET_DATA,
        )


def test_identity_and_fingerprint_are_deterministic_and_source_scoped() -> None:
    first = _profile(received_at=MOMENT)
    retry = _profile(received_at=MOMENT + timedelta(days=5))
    assert first.fingerprint == retry.fingerprint
    assert first.profile_id == retry.profile_id
    assert _instrument().instrument_id != _instrument(source_instrument_id="ACME-ADR").instrument_id
    assert _instrument().instrument_id != _instrument(source_issuer_id="OTHER").instrument_id
    assert _profile(issuer_display_name="Other Issuer").profile_id != first.profile_id
    # Source symbols are preserved as reported; no alias or entity resolution occurs.
    assert _instrument(source_symbol="ACME.A").instrument_id != _instrument(source_symbol="ACME").instrument_id


def test_store_handles_duplicates_conflicts_eviction_and_bounded_metadata() -> None:
    store = IssuerReferenceStore(
        max_instruments=1,
        max_profiles_per_instrument=1,
        max_versions_per_profile=1,
        max_version_identities=2,
    )
    first = _profile(0)
    assert store.ingest(first) is not None
    assert store.ingest(_profile(0, received_at=MOMENT + timedelta(days=8))) is not None
    assert store.profile_count == 1
    assert store.ingest(_profile(1)) is not None
    assert store.profile_count == 1
    assert store.ingest(_profile(0, received_at=MOMENT + timedelta(days=9))) is None
    with pytest.raises(ValueError, match="conflicting issuer-reference"):
        store.ingest(_profile(0, issuer_display_name="Conflicting Acme"))
    assert store.ingest(_profile(2, source_profile_id="other-profile")) is not None
    original = _profile(3, source_profile_id="revision")
    assert store.ingest(original) is not None
    assert store.ingest(_profile(3, source_profile_id="revision", version=2)) is not None
    other_source = IssuerReferenceSource(
        source_id="other-reference-source",
        display_name="Other Reference Source",
        transport=SourceTransport.MANUAL,
        domain=DataDomain.FUNDAMENTAL_DATA,
    )
    assert store.ingest(
        _profile(
            source=other_source,
            instrument=_instrument(source_id=other_source.source_id),
            issuer_display_name="Conflicting Acme",
        )
    ) is not None
    assert store.version_identity_count <= 2


def test_revision_history_snapshot_isolation_and_concurrent_ingestion_are_deterministic() -> None:
    original = _profile(source_profile_id="revisable")
    revision = _profile(
        source_profile_id="revisable",
        version=2,
        issuer_display_name="Acme Holdings Revised",
        revision_of_profile_id=original.profile_id,
    )
    store = IssuerReferenceStore(max_profiles_per_instrument=3)
    store.ingest(original)
    store.ingest(revision)
    history = store.revision_history(original.instrument, "revisable")
    assert tuple(item.profile_version for item in history) == (1, 2)
    assert history[0].issuer_display_name == "Acme Holdings"
    with pytest.raises(FrozenInstanceError):
        history[0].issuer_display_name = "Mutated"  # type: ignore[misc]

    profiles = tuple(_profile(index) for index in range(8))
    first = IssuerReferenceStore(max_profiles_per_instrument=3)
    second = IssuerReferenceStore(max_profiles_per_instrument=3)
    for item in profiles:
        first.ingest(item)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tuple(pool.map(second.ingest, reversed(profiles)))
    assert first.snapshot() == second.snapshot()
    snapshot = first.snapshot(symbol="ACME")
    first.ingest(_profile(12))
    assert snapshot != first.snapshot()


def test_exact_routing_and_ledger_provenance_are_enforced() -> None:
    store = EvidenceStore()
    profile = _profile()
    store.ingest_issuer_reference(profile)
    snapshot = store.snapshot(symbol="ACME")
    router = EvidenceRouter()
    assert len(router.route(agent_id="athena_analyst", snapshot=snapshot).issuer_references) == 1
    assert len(router.route(agent_id="scout_recon", snapshot=snapshot).issuer_references) == 1
    ledger = router.route(agent_id="ledger_the_book", snapshot=snapshot)
    assert ledger.issuer_references == ()
    assert {item.event_id for item in ledger.provenance} == {profile.profile_id}
    for agent_id in (
        "atlas_macro",
        "capitol_smart_money",
        "chartist_technician",
        "oracle_quant",
        "sentinel_risk_officer",
        "pilot_execution",
    ):
        assert router.route(agent_id=agent_id, snapshot=snapshot).issuer_references == ()


def test_core_uses_one_snapshot_and_artifacts_remain_factual() -> None:
    class RecordingRouter(EvidenceRouter):
        def __init__(self) -> None:
            self.snapshot_ids: list[int] = []

        def route(self, *, agent_id: str, snapshot):  # type: ignore[no-untyped-def]
            self.snapshot_ids.append(id(snapshot))
            return super().route(agent_id=agent_id, snapshot=snapshot)

    store = EvidenceStore()
    store.ingest_issuer_reference(_profile())
    router = RecordingRouter()
    core = FridayCore(
        specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store, evidence_router=router
    )
    snapshot = store.snapshot(symbol="ACME")
    run = core._analyze_run(  # noqa: SLF001 - validates the internal snapshot boundary.
        AnalyzeRequest(symbol="ACME", query="fundamental issuer reference review"),
        snapshot=snapshot,
        lifecycle=AnalysisLifecycle(),
    )
    artifacts = {item.agent_id: item for item in run.specialist_analyses}
    assert set(router.snapshot_ids) == {id(snapshot)}
    assert len(artifacts["athena_analyst"].issuer_reference_observations) == 1
    assert len(artifacts["scout_recon"].issuer_reference_observations) == 1
    assert artifacts["ledger_the_book"].issuer_reference_observations == ()
    assert artifacts["athena_analyst"].stance is SpecialistStance.INSUFFICIENT_DATA
    assert not hasattr(artifacts["athena_analyst"].issuer_reference_observations[0], "direction")
    assert artifacts["pilot_execution"].operational_state is SpecialistOperationalState.DISABLED_NO_EXECUTION
    assert profile_id_in_bundle(_profile().profile_id, build_evidence_bundle(snapshot))


def profile_id_in_bundle(profile_id: str, bundle: object) -> bool:
    return any(reference.event_id == profile_id for reference in bundle.references)  # type: ignore[attr-defined]


def test_profiles_cannot_satisfy_sentinel_or_enable_pilot_and_api_stays_compatible() -> None:
    store = EvidenceStore()
    store.ingest_issuer_reference(_profile())
    core = FridayCore(specialists=create_specialist_agents(), master=MasterAgent(), evidence_store=store)
    request = AnalyzeRequest(symbol="ACME", query="issuer profile execution review")
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


def test_ingestion_service_is_provider_neutral_without_a_network_connector() -> None:
    store = EvidenceStore()

    class Consumer:
        def ingest_issuer_reference(
            self,
            profile: NormalizedIssuerInstrumentReference,
        ) -> object:
            return store.ingest_issuer_reference(profile)

    service = IssuerReferenceIngestionService(consumer=Consumer())
    result = service.ingest(_profile())
    assert result == store.snapshot(symbol="ACME").issuer_references[0]
    with pytest.raises(TypeError, match="NormalizedIssuerInstrumentReference"):
        service.ingest(object())  # type: ignore[arg-type]
