"""Atomic, non-submitting shared lifecycle tests using synthetic local facts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import sqlite3

import pytest

from app.execution.shared_authority_store import (
    SharedAuthorityConflict,
    SharedAuthorityStore,
    SharedAuthorityUnavailable,
    _decision_fingerprint,
)
from app.models.demo_authorization import ControlledDemoAuthorization
from app.models.demo_execution import DemoInstrumentBinding
from app.models.durable_execution import (
    DurableAuthorizationConsumptionState,
    DurableBrokerInstrumentEvidence,
    DurableDemoAuthorizationEvidence,
    DurableExecutionReservation,
    DurableMarketReferenceEvidence,
    DurablePortfolioRiskEvidence,
    DurableReservationStatus,
    DurableSafetyEvidence,
    DurableSentinelApprovalEvidence,
)
from app.models.portfolio_risk import SourceAccountKey
from app.models.proposals import SentinelDecision, SentinelDecisionStatus, TradeDirection


NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
ACCOUNT = SourceAccountKey(
    source_id="mt5",
    source_account_id="demo-123",
    account_reference="Demo-Server",
)


def _decision(*, expires_at: datetime | None = None) -> SentinelDecision:
    return SentinelDecision(
        decision_id="decision-1",
        proposal_id="proposal-1",
        proposal_fingerprint="a" * 64,
        analysis_id="analysis-1",
        decision=SentinelDecisionStatus.APPROVED,
        evidence_fingerprint="b" * 64,
        evaluated_at=NOW - timedelta(seconds=5),
        expires_at=expires_at or NOW + timedelta(minutes=5),
        policy_version="v1",
        policy_id="policy-1",
        policy_fingerprint="c" * 64,
        assessment_id="assessment-1",
        assessment_fingerprint="d" * 64,
        account_id=ACCOUNT.account_id,
        approval_id="approval-1",
    )


def _authorization(
    decision: SentinelDecision, *, expires_at: datetime | None = None
) -> ControlledDemoAuthorization:
    return ControlledDemoAuthorization(
        policy_id=decision.policy_id,
        policy_version=decision.policy_version,
        policy_fingerprint=decision.policy_fingerprint,
        account=ACCOUNT,
        instrument=DemoInstrumentBinding(
            canonical_symbol="XAUUSD", broker_symbol="XAUUSD"
        ),
        proposal_id=decision.proposal_id,
        proposal_fingerprint=decision.proposal_fingerprint,
        analysis_id=decision.analysis_id,
        evidence_fingerprint=decision.evidence_fingerprint,
        approval_id=decision.approval_id or "",
        execution_intent_id="intent-1",
        execution_intent_fingerprint="e" * 64,
        direction=TradeDirection.LONG,
        quantity=0.01,
        order_type="MARKET",
        filling_mode="FOK",
        created_at=NOW,
        execution_window_start=NOW - timedelta(seconds=1),
        execution_window_end=NOW + timedelta(minutes=2),
        expires_at=expires_at or NOW + timedelta(minutes=1),
    )


def _reservation(
    decision: SentinelDecision, authorization: ControlledDemoAuthorization
) -> DurableExecutionReservation:
    expires_at = min(
        decision.expires_at or NOW,
        authorization.expires_at,
        NOW + timedelta(seconds=45),
    )
    approval_fingerprint = _decision_fingerprint(decision)
    safety = DurableSafetyEvidence(
        broker_instrument=DurableBrokerInstrumentEvidence(
            source_id="synthetic-read-only-instrument",
            source_fingerprint="f" * 64,
            canonical_symbol="XAUUSD",
            broker_symbol="XAUUSD",
            minimum_volume=Decimal("0.01"),
            maximum_volume=Decimal("1.00"),
            volume_step=Decimal("0.01"),
            price_increment=Decimal("0.01"),
            observed_at=NOW - timedelta(seconds=1),
            expires_at=expires_at,
        ),
        market_reference=DurableMarketReferenceEvidence(
            source_id="synthetic-read-only-market",
            source_fingerprint="1" * 64,
            canonical_symbol="XAUUSD",
            reference_price=Decimal("2350.00"),
            observed_at=NOW - timedelta(seconds=1),
            max_age_seconds=30,
        ),
        portfolio_risk=DurablePortfolioRiskEvidence(
            source_id="synthetic-read-only-risk",
            snapshot_id="risk-1",
            snapshot_fingerprint="2" * 64,
            account_id=ACCOUNT.account_id,
            account_reference=ACCOUNT.source_account_id,
            broker_server=ACCOUNT.account_reference or "",
            risk_policy_fingerprint=decision.policy_fingerprint,
            max_open_positions=1,
            current_open_positions=0,
            max_total_open_volume=Decimal("0.01"),
            current_open_volume=Decimal("0.00"),
            max_order_volume=Decimal("0.01"),
            status="clear",
            observed_at=NOW - timedelta(seconds=1),
            expires_at=expires_at,
        ),
        sentinel_approval=DurableSentinelApprovalEvidence(
            decision_id=decision.decision_id,
            approval_id=decision.approval_id or "",
            approval_fingerprint=approval_fingerprint,
            proposal_id=decision.proposal_id,
            proposal_fingerprint=decision.proposal_fingerprint,
            analysis_id=decision.analysis_id,
            evidence_fingerprint=decision.evidence_fingerprint,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            policy_fingerprint=decision.policy_fingerprint,
            assessment_id=decision.assessment_id,
            assessment_fingerprint=decision.assessment_fingerprint,
            account_id=decision.account_id or "",
            status="approved",
            evaluated_at=decision.evaluated_at,
            expires_at=decision.expires_at or expires_at,
        ),
        demo_authorization=DurableDemoAuthorizationEvidence(
            authorization_id=authorization.authorization_id,
            authorization_fingerprint=authorization.fingerprint,
            policy_id=authorization.policy_id,
            policy_fingerprint=authorization.policy_fingerprint,
            account_id=authorization.account.account_id,
            account_reference=authorization.account.source_account_id,
            broker_server=authorization.account.account_reference or "",
            proposal_id=authorization.proposal_id,
            proposal_fingerprint=authorization.proposal_fingerprint,
            analysis_id=authorization.analysis_id,
            evidence_fingerprint=authorization.evidence_fingerprint,
            execution_intent_id=authorization.execution_intent_id,
            execution_intent_fingerprint=authorization.execution_intent_fingerprint,
            canonical_symbol=authorization.instrument.canonical_symbol,
            broker_symbol=authorization.instrument.broker_symbol,
            status="authorized",
            issued_at=authorization.created_at,
            expires_at=authorization.expires_at,
        ),
    )
    return DurableExecutionReservation(
        policy_version=decision.policy_version,
        account_id=authorization.account.account_id,
        account_reference=authorization.account.source_account_id,
        broker_server=authorization.account.account_reference or "",
        proposal_id=decision.proposal_id,
        analysis_id=decision.analysis_id,
        proposal_fingerprint=decision.proposal_fingerprint,
        evidence_fingerprint=decision.evidence_fingerprint,
        sentinel_approval_id=decision.approval_id or "",
        sentinel_approval_fingerprint=approval_fingerprint,
        sentinel_policy_id=decision.policy_id,
        controlled_demo_authorization_id=authorization.authorization_id,
        controlled_demo_authorization_fingerprint=authorization.fingerprint,
        execution_intent_id=authorization.execution_intent_id,
        execution_intent_fingerprint=authorization.execution_intent_fingerprint,
        canonical_symbol=authorization.instrument.canonical_symbol,
        broker_symbol=authorization.instrument.broker_symbol,
        direction=authorization.direction,
        quantity=Decimal(str(authorization.quantity)),
        order_type=authorization.order_type,
        filling_mode=authorization.filling_mode,
        stop_loss=Decimal("2300.00"),
        take_profit=Decimal("2400.00"),
        safety_evidence=safety,
        created_at=NOW,
        expires_at=expires_at,
        audit_correlation_id="shared-test-only-reservation",
    )


def _prepared_store(path):
    store = SharedAuthorityStore(path)
    decision = _decision()
    authorization = _authorization(decision)
    store._persist_sentinel_evaluation(decision, now=NOW)
    store._persist_demo_authorization(authorization, decision, now=NOW)
    return store, decision, authorization, _reservation(decision, authorization)


def _statuses(path) -> dict[str, str]:
    connection = sqlite3.connect(path)
    try:
        return dict(
            connection.execute(
                "SELECT entity_kind, status FROM shared_authority_entities "
                "WHERE entity_kind != 'schema_bootstrap' ORDER BY entity_kind"
            ).fetchall()
        )
    finally:
        connection.close()


def test_atomic_test_only_reservation_consumes_both_authorities(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    store, decision, authorization, reservation = _prepared_store(path)

    committed, created = store._commit_test_only_reservation(
        reservation, decision=decision, authorization=authorization, now=NOW
    )
    repeat, repeated = store._commit_test_only_reservation(
        reservation, decision=decision, authorization=authorization, now=NOW
    )

    assert created is True and repeated is False and repeat == committed
    assert committed.test_only is True
    assert committed.status is DurableReservationStatus.READY_FOR_ATTEMPT
    assert (
        committed.authorization_consumption
        is DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED
    )
    assert _statuses(path) == {
        "demo_authorization": "consumed",
        "execution_reservation": "ready_for_attempt",
        "sentinel_approval": "consumed",
    }
    with pytest.raises(SharedAuthorityConflict, match="CONSUMED"):
        store._verify_sentinel_approval(decision, now=NOW)
    with pytest.raises(SharedAuthorityConflict, match="CONSUMED"):
        store._verify_demo_authorization(authorization, now=NOW)


def test_atomic_commit_failure_rolls_back_all_three_records(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "shared.sqlite3"
    store, decision, authorization, reservation = _prepared_store(path)

    def fail_commit(_connection) -> None:
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(store, "_commit", fail_commit)
    with pytest.raises(SharedAuthorityUnavailable):
        store._commit_test_only_reservation(
            reservation, decision=decision, authorization=authorization, now=NOW
        )

    assert _statuses(path) == {
        "demo_authorization": "active",
        "sentinel_approval": "active",
    }
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM shared_execution_reservations"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_concurrent_store_instances_are_idempotent_not_double_consuming(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    _store, decision, authorization, reservation = _prepared_store(path)

    def commit(_index: int):
        return SharedAuthorityStore(path)._commit_test_only_reservation(
            reservation, decision=decision, authorization=authorization, now=NOW
        )[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(commit, (1, 2)))
    assert sorted(outcomes) == [False, True]
    assert _statuses(path)["execution_reservation"] == "ready_for_attempt"


def test_lineage_tampering_fails_before_any_state_change(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    store, decision, authorization, reservation = _prepared_store(path)
    tampered = reservation.model_copy(update={"broker_server": "Other-Server"})

    with pytest.raises(SharedAuthorityConflict, match="RESERVATION_INVALID"):
        store._commit_test_only_reservation(
            tampered, decision=decision, authorization=authorization, now=NOW
        )
    assert _statuses(path) == {
        "demo_authorization": "active",
        "sentinel_approval": "active",
    }


def test_progression_revalidates_market_freshness_without_consuming(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    store, decision, authorization, reservation = _prepared_store(path)

    with pytest.raises(SharedAuthorityConflict, match="EVIDENCE_UNAVAILABLE"):
        store._commit_test_only_reservation(
            reservation,
            decision=decision,
            authorization=authorization,
            now=NOW + timedelta(seconds=31),
        )
    assert _statuses(path) == {
        "demo_authorization": "active",
        "sentinel_approval": "active",
    }


def test_restart_recovery_is_idempotent_and_never_retries(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    store, decision, authorization, reservation = _prepared_store(path)
    committed, _ = store._commit_test_only_reservation(
        reservation, decision=decision, authorization=authorization, now=NOW
    )

    reopened = SharedAuthorityStore(path)
    recovered = reopened.recover_non_submitting_lifecycle(
        now=NOW + timedelta(seconds=1)
    )
    assert recovered == (
        (
            "shared-execution-reservation-" + committed.reservation_id,
            "reconciliation_required",
        ),
    )
    assert reopened.recover_non_submitting_lifecycle(
        now=NOW + timedelta(seconds=2)
    ) == ()
    assert _statuses(path)["execution_reservation"] == "reconciliation_required"
    with pytest.raises(SharedAuthorityConflict, match="RECONCILIATION_REQUIRED"):
        reopened._commit_test_only_reservation(
            reservation,
            decision=decision,
            authorization=authorization,
            now=NOW + timedelta(seconds=2),
        )


def test_expired_authority_recovery_persists_terminal_state(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    expiry = NOW + timedelta(seconds=10)
    decision = _decision(expires_at=expiry)
    authorization = _authorization(decision, expires_at=expiry)
    store = SharedAuthorityStore(path)
    store._persist_sentinel_evaluation(decision, now=NOW)
    store._persist_demo_authorization(authorization, decision, now=NOW)

    recovered = store.recover_non_submitting_lifecycle(
        now=expiry + timedelta(seconds=1)
    )
    assert len(recovered) == 2
    assert _statuses(path) == {
        "demo_authorization": "expired",
        "sentinel_approval": "expired",
    }
    assert SharedAuthorityStore(path).recover_non_submitting_lifecycle(
        now=expiry + timedelta(seconds=2)
    ) == ()


def test_shared_lifecycle_has_no_submission_or_broker_dependency() -> None:
    import app.execution.shared_authority_store as module

    source = inspect.getsource(module)
    for forbidden in (
        "order_send",
        "MetaTrader5",
        "MT5ExecutionAdapter",
        "PilotExecutionCoordinator",
        "FinalExecutionGate",
        ".submit(",
    ):
        assert forbidden not in source
