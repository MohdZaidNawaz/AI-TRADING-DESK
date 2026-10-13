"""Phase 13C-2 tests for trusted Sentinel evaluation and durable ownership."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.evidence import EvidenceStore
from app.execution.shared_authority_store import SharedAuthorityStore
from app.models.events import (
    AssetClass,
    DataDomain,
    EventType,
    NormalizedDataEvent,
    SourceMetadata,
    SourceTransport,
)
from app.models.proposals import (
    PortfolioRiskContext,
    PositionSizingIntent,
    ProposedTradeParameters,
    RiskContext,
    RiskReward,
    SentinelDecisionStatus,
    SpecialistSignal,
    TradeDirection,
    TradeProposal,
    TradeProposalStatus,
)
from app.services.trade_proposal_workflow import ApprovalClaimError, ApprovalRegistry, SentinelRiskGate
from app.services.trusted_sentinel_issuance import TrustedSentinelIssuanceService


MOMENT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SOURCE = SourceMetadata(
    source_id="phase13c2_market",
    display_name="Phase 13C-2 Market",
    transport=SourceTransport.MANUAL,
    domain=DataDomain.MARKET_DATA,
)


def _snapshot():
    store = EvidenceStore()
    store.ingest_market_event(
        NormalizedDataEvent(
            event_id="phase13c2-market-event",
            source=SOURCE,
            event_type=EventType.MARKET_QUOTE,
            event="market_snapshot",
            asset_class=AssetClass.COMMODITY,
            symbol="XAUUSD",
            price=100.0,
            close=100.0,
            timestamp=MOMENT,
            raw_payload={},
        )
    )
    return store.snapshot(symbol="XAUUSD")


def _parameters(*, direction: TradeDirection | None = TradeDirection.LONG) -> ProposedTradeParameters:
    return ProposedTradeParameters(
        direction=direction,
        reference_price=100.0,
        stop_loss=95.0,
        targets=(110.0,),
        risk_reward=RiskReward(risk_distance=5.0, reward_distance=10.0, ratio=2.0),
        position_sizing_intent=PositionSizingIntent.RISK_FRACTION,
        requested_risk_fraction=0.01,
        quantity=1.0,
        order_type="MARKET",
        filling_mode="FOK",
    )


def _risk_context() -> RiskContext:
    return RiskContext(
        as_of=MOMENT.replace(minute=1),
        max_market_evidence_age_seconds=600,
        approval_ttl_seconds=120,
        portfolio=PortfolioRiskContext(
            account_equity=100_000.0,
            current_exposure_fraction=0.10,
            max_total_exposure_fraction=0.50,
        ),
        max_risk_fraction=0.02,
        required_agreeing_specialists=2,
        reject_conflicting_signals=True,
        specialist_signals=(
            SpecialistSignal(
                agent_id="chartist_technician", direction=TradeDirection.LONG, confidence=0.8
            ),
            SpecialistSignal(
                agent_id="oracle_quant", direction=TradeDirection.LONG, confidence=0.7
            ),
        ),
        news_risk_assessed=True,
    )


def _proposal(*, snapshot, parameters: ProposedTradeParameters | None = None) -> TradeProposal:
    from app.services.trade_proposal_workflow import build_evidence_bundle

    bundle = build_evidence_bundle(snapshot)
    return TradeProposal(
        proposal_id="phase13c2-proposal",
        analysis_id="phase13c2-analysis",
        evidence_fingerprint=bundle.fingerprint,
        evidence_references=bundle.references,
        symbol="XAUUSD",
        asset_class=AssetClass.COMMODITY,
        parameters=parameters or _parameters(),
        thesis="Explicit test proposal.",
        created_at=bundle.created_at,
        status=TradeProposalStatus.PENDING_RISK,
    )


def _gate(path: Path) -> tuple[SentinelRiskGate, ApprovalRegistry]:
    registry = ApprovalRegistry(authority_store=SharedAuthorityStore(path))
    return SentinelRiskGate(registry=registry), registry


def test_real_sentinel_approval_is_durable_and_verifies_after_reopen(tmp_path: Path) -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()
    gate, registry = _gate(tmp_path / "authority.sqlite3")

    decision = gate.evaluate(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=risk_context,
    )

    assert decision.decision is SentinelDecisionStatus.APPROVED
    assert decision.approval_id is not None
    reopened = ApprovalRegistry(authority_store=SharedAuthorityStore(tmp_path / "authority.sqlite3"))
    verified = reopened.verify(proposal=proposal, decision=decision, now=risk_context.as_of)
    assert verified.approval_id == decision.approval_id
    # Verification is read-only: an independent reopening sees the same record.
    assert reopened.verify(proposal=proposal, decision=decision, now=risk_context.as_of).approval_id == decision.approval_id
    assert registry.authority_integration_state == "SHARED_DURABLE_SENTINEL_APPROVAL_OWNER"


def test_rejected_or_insufficient_evaluation_cannot_issue_an_approval(tmp_path: Path) -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot, parameters=_parameters(direction=None))
    gate, registry = _gate(tmp_path / "authority.sqlite3")

    decision = gate.evaluate(proposal=proposal, snapshot=snapshot, risk_context=_risk_context())

    assert decision.decision is SentinelDecisionStatus.INSUFFICIENT_DATA
    assert decision.approval_id is None
    with pytest.raises(ApprovalClaimError):
        registry.verify(proposal=proposal, decision=decision, now=_risk_context().as_of)


def test_fabricated_decision_has_no_public_issuance_path_and_is_not_registered(tmp_path: Path) -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()
    gate, registry = _gate(tmp_path / "authority.sqlite3")
    derived, _assessment = gate._evaluate_unissued(
        proposal=proposal,
        snapshot=snapshot,
        risk_context=risk_context,
    )
    fabricated = derived.model_copy(update={"approval_id": "fabricated-approval"})

    assert "decision" not in inspect.signature(
        TrustedSentinelIssuanceService.evaluate_and_issue
    ).parameters
    with pytest.raises(ApprovalClaimError, match="unavailable"):
        registry.verify(proposal=proposal, decision=fabricated, now=risk_context.as_of)


def test_revocation_survives_reopen_and_blocks_verification(tmp_path: Path) -> None:
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()
    gate, registry = _gate(tmp_path / "authority.sqlite3")
    decision = gate.evaluate(proposal=proposal, snapshot=snapshot, risk_context=risk_context)
    assert decision.approval_id is not None

    registry.revoke(approval_id=decision.approval_id)
    reopened = ApprovalRegistry(authority_store=SharedAuthorityStore(tmp_path / "authority.sqlite3"))
    with pytest.raises(ApprovalClaimError, match="revoked"):
        reopened.verify(proposal=proposal, decision=decision, now=risk_context.as_of)


def test_issued_lifecycle_and_outbox_are_atomic_and_verification_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "authority.sqlite3"
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()
    gate, registry = _gate(path)

    decision = gate.evaluate(proposal=proposal, snapshot=snapshot, risk_context=risk_context)
    before = SharedAuthorityStore(path).lifecycle_events()
    registry.verify(proposal=proposal, decision=decision, now=risk_context.as_of)
    after = SharedAuthorityStore(path).lifecycle_events()

    assert len(before) == len(after) == 1
    assert before[0].event_state == "sentinel_approval_issued"
    assert before[0].entity_kind.value == "sentinel_approval"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shared_sentinel_approvals").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM shared_authority_outbox").fetchone() == (1,)


def test_issuance_commit_failure_leaves_no_partial_durable_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "authority.sqlite3"
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()
    store = SharedAuthorityStore(path)
    gate = SentinelRiskGate(registry=ApprovalRegistry(authority_store=store))

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("injected commit failure")

    monkeypatch.setattr(store, "_commit", fail_commit)
    with pytest.raises(ApprovalClaimError, match="DURABLE_SENTINEL_APPROVAL_UNAVAILABLE"):
        gate.evaluate(proposal=proposal, snapshot=snapshot, risk_context=risk_context)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shared_sentinel_approvals").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM shared_authority_lifecycle_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM shared_authority_outbox").fetchone() == (0,)


def test_concurrent_instances_issue_one_idempotent_approval(tmp_path: Path) -> None:
    path = tmp_path / "authority.sqlite3"
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()

    def issue_once() -> str | None:
        gate, _registry = _gate(path)
        return gate.evaluate(
            proposal=proposal,
            snapshot=snapshot,
            risk_context=risk_context,
        ).approval_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_ids = tuple(executor.map(lambda _item: issue_once(), range(2)))

    assert approval_ids[0] is not None
    assert approval_ids[0] == approval_ids[1]
    assert len(SharedAuthorityStore(path).lifecycle_events()) == 1


def test_expiry_and_lineage_tampering_fail_closed_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "authority.sqlite3"
    snapshot = _snapshot()
    proposal = _proposal(snapshot=snapshot)
    risk_context = _risk_context()
    gate, _registry = _gate(path)
    decision = gate.evaluate(proposal=proposal, snapshot=snapshot, risk_context=risk_context)
    assert decision.expires_at is not None

    reopened = ApprovalRegistry(authority_store=SharedAuthorityStore(path))
    tampered = decision.model_copy(update={"policy_fingerprint": "0" * 64})
    with pytest.raises(ApprovalClaimError, match="unavailable"):
        reopened.verify(proposal=proposal, decision=tampered, now=risk_context.as_of)
    with pytest.raises(ApprovalClaimError, match="expired"):
        reopened.verify(proposal=proposal, decision=decision, now=decision.expires_at)
    after_expiry = ApprovalRegistry(authority_store=SharedAuthorityStore(path))
    with pytest.raises(ApprovalClaimError, match="expired"):
        after_expiry.verify(proposal=proposal, decision=decision, now=risk_context.as_of)


def test_trusted_issuance_service_has_no_execution_dependencies() -> None:
    source = inspect.getsource(TrustedSentinelIssuanceService)
    for forbidden in ("Pilot", "MetaTrader5", "order_send", "submit", "adapter"):
        assert forbidden not in source
