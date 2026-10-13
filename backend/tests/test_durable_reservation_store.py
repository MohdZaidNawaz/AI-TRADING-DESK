"""Phase 13 tests. Every boundary is local; no adapter or broker is invoked."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
from pathlib import Path
import sqlite3

import pytest

from app.audit.service import AuditService
from app.audit.store import AuditStore, AuditStoreError
from app.execution.durable_execution_recovery import DurableExecutionRecoveryService
from app.execution.durable_reservation_store import (
    DurableReservationConflict,
    DurableTestOnlySafetyEvidenceEvaluator,
    DurableReservationStateError,
    DurableReservationStore,
    DurableReservationUnavailable,
)
from app.models.durable_execution import (
    DurableAuthorizationConsumptionState,
    DurableBrokerInstrumentEvidence,
    DurableDemoAuthorizationEvidence,
    DurableExecutionReservation,
    DurableMarketReferenceEvidence,
    DurablePortfolioRiskEvidence,
    DurableReservationStatus,
    DurableTestOnlyReservationEvaluation,
    DurableVerificationCheck,
    DurableSafetyEvidence,
    DurableSentinelApprovalEvidence,
)
from app.models.proposals import TradeDirection


NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


class _TestOnlyEvidenceEvaluator(DurableTestOnlySafetyEvidenceEvaluator):
    """Synthetic structural evaluator for isolated Phase 13 tests only."""

    def verify(self, reservation: DurableExecutionReservation, *, now: datetime) -> DurableTestOnlyReservationEvaluation:
        return DurableTestOnlyReservationEvaluation(
            reservation_id=reservation.reservation_id,
            reservation_fingerprint=reservation.fingerprint,
            safety_evidence_fingerprint=reservation.safety_evidence.fingerprint,
            verified_at=now,
            outcome="test_only_validated",
            checks=tuple(DurableVerificationCheck),
        )


class _RejectedEvidence(DurableTestOnlySafetyEvidenceEvaluator):
    def verify(self, reservation: DurableExecutionReservation, *, now: datetime) -> DurableTestOnlyReservationEvaluation:
        del reservation, now
        raise RuntimeError("source unavailable")


class _MalformedEvidence(DurableTestOnlySafetyEvidenceEvaluator):
    def verify(self, reservation: DurableExecutionReservation, *, now: datetime):
        del reservation, now
        return ""


class _MutableEvidence(_TestOnlyEvidenceEvaluator):
    valid = True

    def verify(self, reservation: DurableExecutionReservation, *, now: datetime) -> DurableTestOnlyReservationEvaluation:
        if not self.valid:
            raise RuntimeError("source state revoked")
        return super().verify(reservation, now=now)


def _safety(*, market_age: int = 60, instrument_minimum: Decimal = Decimal("0.01"), instrument_maximum: Decimal = Decimal("10.00"), instrument_step: Decimal = Decimal("0.01"), risk_account: str = "account-1", authorization_id: str = "authorization-1") -> DurableSafetyEvidence:
    expiry = NOW + timedelta(minutes=10)
    return DurableSafetyEvidence(
        broker_instrument=DurableBrokerInstrumentEvidence(
            source_id="broker-metadata", source_fingerprint="d" * 64,
            canonical_symbol="XAUUSD", broker_symbol="XAUUSD",
            minimum_volume=instrument_minimum, maximum_volume=instrument_maximum,
            volume_step=instrument_step, price_increment=Decimal("0.01"),
            observed_at=NOW - timedelta(seconds=10), expires_at=expiry,
        ),
        market_reference=DurableMarketReferenceEvidence(
            source_id="market-snapshot", source_fingerprint="e" * 64,
            canonical_symbol="XAUUSD", reference_price=Decimal("2350.00"),
            observed_at=NOW - timedelta(seconds=market_age), max_age_seconds=60,
        ),
        portfolio_risk=DurablePortfolioRiskEvidence(
            source_id="portfolio-risk", snapshot_id="risk-snapshot-1", snapshot_fingerprint="f" * 64,
            account_id=risk_account, account_reference="server:account-1", broker_server="server",
            risk_policy_fingerprint="5" * 64, max_open_positions=1, current_open_positions=0,
            max_total_open_volume=Decimal("0.01"), current_open_volume=Decimal("0.00"),
            max_order_volume=Decimal("0.01"), status="clear", observed_at=NOW - timedelta(seconds=10), expires_at=expiry,
        ),
        sentinel_approval=DurableSentinelApprovalEvidence(
            decision_id="decision-1", approval_id="approval-1", approval_fingerprint="1" * 64,
            proposal_id="proposal-1", proposal_fingerprint="a" * 64, analysis_id="analysis-1",
            evidence_fingerprint="b" * 64, policy_id="policy-1", policy_version="v1",
            policy_fingerprint="2" * 64, assessment_id="assessment-1", assessment_fingerprint="3" * 64,
            account_id="account-1", status="approved", evaluated_at=NOW - timedelta(seconds=10), expires_at=expiry,
        ),
        demo_authorization=DurableDemoAuthorizationEvidence(
            authorization_id=authorization_id, authorization_fingerprint="4" * 64,
            policy_id="policy-1", policy_fingerprint="2" * 64, account_id="account-1",
            account_reference="server:account-1", broker_server="server", proposal_id="proposal-1",
            proposal_fingerprint="a" * 64, analysis_id="analysis-1", evidence_fingerprint="b" * 64,
            execution_intent_id="intent-1", execution_intent_fingerprint="c" * 64,
            canonical_symbol="XAUUSD", broker_symbol="XAUUSD", status="authorized",
            issued_at=NOW - timedelta(seconds=10), expires_at=expiry,
        ),
    )


def _reservation(*, intent: str = "intent-1", safety: DurableSafetyEvidence | None = None, expires_at: datetime | None = None) -> DurableExecutionReservation:
    safe = safety or _safety()
    if intent != "intent-1":
        authorization = safe.demo_authorization.model_copy(update={"execution_intent_id": intent})
        safe = safe.model_copy(update={"demo_authorization": authorization})
    return DurableExecutionReservation(
        policy_version="v1", account_id="account-1", account_reference="server:account-1", broker_server="server",
        proposal_id="proposal-1", analysis_id="analysis-1", proposal_fingerprint="a" * 64,
        evidence_fingerprint="b" * 64, sentinel_approval_id="approval-1", sentinel_approval_fingerprint="1" * 64,
        sentinel_policy_id="policy-1", controlled_demo_authorization_id=safe.demo_authorization.authorization_id,
        controlled_demo_authorization_fingerprint=safe.demo_authorization.authorization_fingerprint, execution_intent_id=intent,
        execution_intent_fingerprint="c" * 64, canonical_symbol="XAUUSD", broker_symbol="XAUUSD",
        direction=TradeDirection.LONG, quantity=Decimal("0.01"), order_type="MARKET", filling_mode="FOK",
        stop_loss=Decimal("2300.00"), take_profit=Decimal("2400.00"), safety_evidence=safe,
        created_at=NOW, expires_at=expires_at or NOW + timedelta(minutes=5), audit_correlation_id="audit-correlation-1",
    )


def _store(tmp_path: Path, *, evaluator=_TestOnlyEvidenceEvaluator()) -> DurableReservationStore:
    return DurableReservationStore(tmp_path / "durable.sqlite3", test_only_evaluator=evaluator)


def _reservation_data(reservation: DurableExecutionReservation | None = None) -> dict:
    data = (reservation or _reservation()).model_dump(mode="json", exclude={"execution_identity", "reservation_id", "fingerprint"})
    data["safety_evidence"].pop("fingerprint", None)
    return data


def _audit(tmp_path: Path) -> AuditService:
    return AuditService(AuditStore(tmp_path / "audit.sqlite3"))


def _ready(store: DurableReservationStore, audit: AuditService, reservation: DurableExecutionReservation):
    stored, created = store.reserve(reservation)
    assert created is True
    assert store.project_outbox(audit) == 1
    return store.promote_ready(stored.reservation_id, now=NOW + timedelta(seconds=1))


def _attempt_started(store: DurableReservationStore, audit: AuditService, reservation: DurableExecutionReservation):
    ready = _ready(store, audit, reservation)
    confirmed = store.confirm_authorization_consumed_for_test_only(ready.reservation_id, now=NOW + timedelta(seconds=2))
    assert confirmed.authorization_consumption is DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED
    assert store.project_outbox(audit) == 2
    return store.start_attempt(ready.reservation_id, now=NOW + timedelta(seconds=3), kill_switch_active=False)


def test_reservation_requires_explicit_test_evaluator_and_persists_bound_evidence(tmp_path) -> None:
    reservation = _reservation()
    with pytest.raises(DurableReservationUnavailable, match="TEST_EVALUATOR_REQUIRED"):
        DurableReservationStore(tmp_path / "blocked.sqlite3")
    store = _store(tmp_path)
    stored, created = store.reserve(reservation)
    assert created is True and stored.safety_evidence.fingerprint == reservation.safety_evidence.fingerprint
    reopened = DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    assert reopened.get(reservation.reservation_id) == reservation
    assert reopened.history(reservation.reservation_id) == ((1, None, "reserved", "durable_reservation_created"),)


def test_test_evaluator_must_be_typed_complete_and_current(tmp_path) -> None:
    reservation = _reservation()
    with pytest.raises(TypeError, match="explicit test-only evaluator"):
        DurableReservationStore(tmp_path / "invalid.sqlite3", test_only_evaluator=object())
    malformed = _store(tmp_path / "malformed", evaluator=_MalformedEvidence())
    with pytest.raises(DurableReservationStateError, match="TEST_EVALUATION_MALFORMED"):
        malformed.reserve(reservation)
    rejected = _store(tmp_path / "rejected", evaluator=_RejectedEvidence())
    with pytest.raises(DurableReservationStateError, match="TEST_EVALUATION_UNAVAILABLE"):
        rejected.reserve(reservation)


def test_arbitrary_test_evaluator_cannot_create_authoritative_proof(tmp_path) -> None:
    """An arbitrary evaluator remains a test-only structural check.

    No trusted source integration exists in Phase 13, so this test documents
    the hard limitation instead of treating a syntactically valid result as an
    authority assertion.
    """
    class _ArbitraryCallerEvaluator(DurableTestOnlySafetyEvidenceEvaluator):
        def verify(self, reservation: DurableExecutionReservation, *, now: datetime) -> DurableTestOnlyReservationEvaluation:
            return DurableTestOnlyReservationEvaluation(
                reservation_id=reservation.reservation_id,
                reservation_fingerprint=reservation.fingerprint,
                safety_evidence_fingerprint=reservation.safety_evidence.fingerprint,
                verified_at=now,
                outcome="test_only_validated",
                checks=tuple(DurableVerificationCheck),
            )

    store = DurableReservationStore(
        tmp_path / "arbitrary.sqlite3",
        test_only_evaluator=_ArbitraryCallerEvaluator(),
    )
    stored, created = store.reserve(_reservation())
    assert created is True
    assert store.test_only is True
    assert stored.test_only is True
    with pytest.raises(ValueError):
        DurableTestOnlyReservationEvaluation(
            reservation_id=stored.reservation_id,
            reservation_fingerprint=stored.fingerprint,
            safety_evidence_fingerprint=stored.safety_evidence.fingerprint,
            verified_at=NOW,
            outcome="verified",
            checks=tuple(DurableVerificationCheck),
        )
    with pytest.raises(ValueError):
        DurableTestOnlyReservationEvaluation(
            reservation_id=stored.reservation_id,
            reservation_fingerprint=stored.fingerprint,
            safety_evidence_fingerprint=stored.safety_evidence.fingerprint,
            verified_at=NOW,
            outcome="test_only_validated",
            checks=tuple(DurableVerificationCheck),
            test_only=False,
        )


def test_failed_revalidation_persists_reconciliation_and_blocks_progression(tmp_path) -> None:
    evaluator = _MutableEvidence()
    store = _store(tmp_path, evaluator=evaluator)
    audit = _audit(tmp_path)
    reservation, _ = store.reserve(_reservation())
    assert store.project_outbox(audit) == 1
    evaluator.valid = False
    with pytest.raises(DurableReservationStateError, match="TEST_EVALUATION_UNAVAILABLE"):
        store.promote_ready(reservation.reservation_id, now=NOW + timedelta(seconds=1))
    assert store.get(reservation.reservation_id).status is DurableReservationStatus.RECONCILIATION_REQUIRED


def test_changed_evidence_fingerprint_cannot_progress_after_reservation(tmp_path) -> None:
    class _SwappedEvidence(_TestOnlyEvidenceEvaluator):
        swapped = False

        def verify(self, reservation: DurableExecutionReservation, *, now: datetime) -> DurableTestOnlyReservationEvaluation:
            result = super().verify(reservation, now=now)
            return result.model_copy(update={"safety_evidence_fingerprint": "9" * 64}) if self.swapped else result

    evaluator = _SwappedEvidence()
    store, audit = _store(tmp_path, evaluator=evaluator), _audit(tmp_path)
    reservation, _ = store.reserve(_reservation())
    assert store.project_outbox(audit) == 1
    evaluator.swapped = True
    with pytest.raises(DurableReservationStateError, match="TEST_EVIDENCE_FINGERPRINT_MISMATCH"):
        store.promote_ready(reservation.reservation_id, now=NOW + timedelta(seconds=1))
    assert store.get(reservation.reservation_id).status is DurableReservationStatus.RECONCILIATION_REQUIRED


def test_decimal_volume_boundaries_and_nontrivial_step_are_fail_closed() -> None:
    assert _reservation(safety=_safety(instrument_minimum=Decimal("0.01"), instrument_maximum=Decimal("0.01"))).quantity == Decimal("0.01")
    with pytest.raises(ValueError, match="volume constraints"):
        _reservation(safety=_safety(instrument_minimum=Decimal("0.005"), instrument_step=Decimal("0.002")))


def test_safety_evidence_is_immutable_and_binds_lineage_and_volume() -> None:
    reservation = _reservation()
    with pytest.raises(Exception):
        reservation.safety_evidence.market_reference.reference_price = Decimal("1")
    changed = _reservation_data(reservation)
    changed["safety_evidence"]["demo_authorization"]["authorization_fingerprint"] = "9" * 64
    with pytest.raises(ValueError, match="authorization lineage"):
        DurableExecutionReservation.model_validate(changed)
    changed = _reservation_data(reservation)
    changed["safety_evidence"]["broker_instrument"]["minimum_volume"] = "0.02"
    with pytest.raises(ValueError, match="volume constraints"):
        DurableExecutionReservation.model_validate(changed)


@pytest.mark.parametrize("change, message", [
    (lambda data: data["safety_evidence"]["market_reference"].update({"observed_at": (NOW - timedelta(seconds=61)).isoformat()}), "stale"),
    (lambda data: data["safety_evidence"]["portfolio_risk"].update({"account_id": "other"}), "account binding"),
    (lambda data: data["safety_evidence"]["sentinel_approval"].update({"proposal_fingerprint": "8" * 64}), "Sentinel lineage"),
    (lambda data: data["safety_evidence"]["demo_authorization"].update({"execution_intent_fingerprint": "7" * 64}), "authorization lineage"),
    (lambda data: data["safety_evidence"]["portfolio_risk"].update({"current_open_positions": 1}), "portfolio risk limits"),
])
def test_adversarial_safety_evidence_is_rejected(change, message) -> None:
    data = _reservation_data()
    change(data)
    with pytest.raises(ValueError, match=message):
        DurableExecutionReservation.model_validate(data)


def test_one_order_identity_and_database_concurrency_are_fail_closed(tmp_path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    reservation = _reservation()

    def same() -> bool:
        return DurableReservationStore(path, test_only_evaluator=_TestOnlyEvidenceEvaluator()).reserve(reservation)[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: same(), range(2))) == [False, True]
    store = DurableReservationStore(path, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    with pytest.raises(DurableReservationStateError, match="FIRST_ORDER"):
        store.reserve(_reservation(intent="intent-2"))
    conflict_data = _reservation_data(reservation)
    conflict_data["proposal_fingerprint"] = "d" * 64
    conflict_data["safety_evidence"]["sentinel_approval"]["proposal_fingerprint"] = "d" * 64
    conflict_data["safety_evidence"]["demo_authorization"]["proposal_fingerprint"] = "d" * 64
    with pytest.raises(DurableReservationConflict):
        store.reserve(DurableExecutionReservation.model_validate(conflict_data))


def test_local_reservation_and_outbox_are_atomic_and_replay_after_restart(tmp_path) -> None:
    store = _store(tmp_path)
    store._fail_next_commit_for_test = True
    with pytest.raises(DurableReservationUnavailable):
        store.reserve(_reservation())
    assert DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator()).count == 0
    reopened = DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    reservation, _ = reopened.reserve(_reservation())
    audit = _audit(tmp_path)
    assert reopened.project_outbox(audit) == 1
    assert DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator()).project_outbox(audit) == 0
    assert len(audit.events()) == 1


def test_partial_outbox_projection_replays_only_remaining_events_after_reopen(tmp_path) -> None:
    store, audit = _store(tmp_path), _audit(tmp_path)
    reservation, _ = store.reserve(_reservation())
    assert store.project_outbox(audit) == 1
    ready = store.promote_ready(reservation.reservation_id, now=NOW + timedelta(seconds=1))
    store.confirm_authorization_consumed_for_test_only(ready.reservation_id, now=NOW + timedelta(seconds=2))

    class _PartialAudit:
        calls = 0

        def record(self, event):
            self.calls += 1
            if self.calls == 2:
                raise AuditStoreError("synthetic second-event failure")
            audit.record(event)

    partial = _PartialAudit()
    with pytest.raises(DurableReservationUnavailable):
        store.project_outbox(partial)
    assert partial.calls == 2
    reopened = DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    assert reopened.project_outbox(audit) == 1
    assert reopened.project_outbox(audit) == 0
    assert len(audit.events()) == 3


def test_audit_failure_or_pending_projection_blocks_readiness_and_attempt(tmp_path) -> None:
    store = _store(tmp_path)
    reservation, _ = store.reserve(_reservation())

    class _FailingAudit:
        def record(self, _event):
            raise AuditStoreError("synthetic audit failure")

    with pytest.raises(DurableReservationUnavailable):
        store.project_outbox(_FailingAudit())
    with pytest.raises(DurableReservationStateError, match="AUDIT_NOT_PROJECTED"):
        store.promote_ready(reservation.reservation_id, now=NOW + timedelta(seconds=1))
    audit = _audit(tmp_path)
    assert store.project_outbox(audit) == 1
    ready = store.promote_ready(reservation.reservation_id, now=NOW + timedelta(seconds=1))
    store.confirm_authorization_consumed_for_test_only(ready.reservation_id, now=NOW + timedelta(seconds=2))
    with pytest.raises(DurableReservationStateError, match="AUDIT_NOT_PROJECTED"):
        store.start_attempt(ready.reservation_id, now=NOW + timedelta(seconds=3), kill_switch_active=False)


def test_complete_lifecycle_table_is_persisted_audited_and_terminal(tmp_path) -> None:
    store, audit = _store(tmp_path), _audit(tmp_path)
    started = _attempt_started(store, audit, _reservation())
    completed = store.complete_terminal(started.reservation_id, now=NOW + timedelta(seconds=4))
    assert completed.status is DurableReservationStatus.COMPLETED_TERMINAL
    assert store.complete_terminal(started.reservation_id, now=NOW + timedelta(seconds=5)) == completed
    with pytest.raises(DurableReservationStateError, match="LIFECYCLE_INVALID"):
        store.reject_terminal(started.reservation_id, now=NOW + timedelta(seconds=6))
    history = DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator()).history(started.reservation_id)
    assert [item[2] for item in history] == ["reserved", "ready_for_attempt", "ready_for_attempt", "attempt_started", "completed_terminal"]
    assert store.project_outbox(audit) == 2


def test_revocation_and_expiry_are_persisted_idempotent_and_block_reopen(tmp_path) -> None:
    store, audit = _store(tmp_path), _audit(tmp_path)
    ready = _ready(store, audit, _reservation())
    revoked = store.revoke(ready.reservation_id, now=NOW + timedelta(seconds=2))
    assert revoked.status is DurableReservationStatus.REVOKED
    assert store.revoke(ready.reservation_id, now=NOW + timedelta(seconds=3)) == revoked
    with pytest.raises(DurableReservationStateError):
        store.promote_ready(ready.reservation_id, now=NOW + timedelta(seconds=4))

    expired_store, expired_audit = _store(tmp_path / "expired"), _audit(tmp_path / "expired")
    expired = _reservation(expires_at=NOW + timedelta(seconds=2))
    expired_ready = _ready(expired_store, expired_audit, expired)
    with pytest.raises(DurableReservationStateError, match="EXPIRED"):
        expired_store.confirm_authorization_consumed_for_test_only(expired_ready.reservation_id, now=NOW + timedelta(seconds=3))
    assert expired_store.get(expired_ready.reservation_id).status is DurableReservationStatus.EXPIRED
    assert expired_store.expire(expired_ready.reservation_id, now=NOW + timedelta(seconds=4)).status is DurableReservationStatus.EXPIRED


def test_reconciliation_unknown_outcome_and_rejected_attempt_never_retry(tmp_path) -> None:
    store, audit = _store(tmp_path), _audit(tmp_path)
    started = _attempt_started(store, audit, _reservation())
    rejected = store.reject_terminal(started.reservation_id, now=NOW + timedelta(seconds=4))
    with pytest.raises(DurableReservationStateError):
        store.start_attempt(rejected.reservation_id, now=NOW + timedelta(seconds=5), kill_switch_active=False)

    uncertain_store, uncertain_audit = _store(tmp_path / "uncertain"), _audit(tmp_path / "uncertain")
    uncertain = _attempt_started(uncertain_store, uncertain_audit, _reservation())
    recovered = DurableExecutionRecoveryService(DurableReservationStore(uncertain_store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())).recover(now=NOW + timedelta(seconds=4))
    assert recovered[0].status is DurableReservationStatus.RECONCILIATION_REQUIRED
    assert DurableExecutionRecoveryService(DurableReservationStore(uncertain_store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())).recover(now=NOW + timedelta(seconds=5)) == ()
    with pytest.raises(DurableReservationStateError):
        uncertain_store.start_attempt(uncertain.reservation_id, now=NOW + timedelta(seconds=6), kill_switch_active=False)


def test_attempt_start_commit_failure_never_reports_success_and_recovery_is_conservative(tmp_path) -> None:
    store, audit = _store(tmp_path), _audit(tmp_path)
    ready = _ready(store, audit, _reservation())
    store.confirm_authorization_consumed_for_test_only(ready.reservation_id, now=NOW + timedelta(seconds=2))
    store.project_outbox(audit)
    store._fail_next_commit_for_test = True
    with pytest.raises(DurableReservationUnavailable):
        store.start_attempt(ready.reservation_id, now=NOW + timedelta(seconds=3), kill_switch_active=False)
    reopened = DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    assert reopened.get(ready.reservation_id).status is DurableReservationStatus.READY_FOR_ATTEMPT
    started = reopened.start_attempt(ready.reservation_id, now=NOW + timedelta(seconds=4), kill_switch_active=False)
    assert started.status is DurableReservationStatus.ATTEMPT_STARTED
    assert DurableExecutionRecoveryService(DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())).recover(now=NOW + timedelta(seconds=5))[0].status is DurableReservationStatus.RECONCILIATION_REQUIRED


def test_readiness_commit_failure_never_reports_success(tmp_path) -> None:
    store, audit = _store(tmp_path), _audit(tmp_path)
    reservation, _ = store.reserve(_reservation())
    assert store.project_outbox(audit) == 1
    store._fail_next_commit_for_test = True
    with pytest.raises(DurableReservationUnavailable):
        store.promote_ready(reservation.reservation_id, now=NOW + timedelta(seconds=1))
    reopened = DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    assert reopened.get(reservation.reservation_id).status is DurableReservationStatus.RESERVED


def test_corrupt_missing_and_unsupported_schema_fail_closed(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(DurableReservationUnavailable):
        DurableReservationStore(corrupt, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    missing = tmp_path / "missing.sqlite3"
    connection = sqlite3.connect(missing)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit(); connection.close()
    with pytest.raises(DurableReservationUnavailable):
        DurableReservationStore(missing, test_only_evaluator=_TestOnlyEvidenceEvaluator())
    store = _store(tmp_path / "unsupported")
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE durable_schema_metadata SET value = '999' WHERE key = 'schema_version'")
    connection.commit(); connection.close()
    with pytest.raises(DurableReservationUnavailable):
        DurableReservationStore(store.path, test_only_evaluator=_TestOnlyEvidenceEvaluator())


def test_no_adapter_mt5_submission_or_production_execution_state_path() -> None:
    import app.execution.durable_execution_recovery as recovery_module
    import app.execution.durable_reservation_store as store_module

    source = inspect.getsource(store_module) + inspect.getsource(recovery_module)
    for forbidden in ("order_send", "MT5ExecutionAdapter", ".submit(", "FinalExecutionGate", "ExecutionStateStore"):
        assert forbidden not in source
    fields = DurableExecutionReservation.model_fields
    assert not {"password", "secret", "api_key", "raw_payload", "broker_response"}.intersection(fields)
