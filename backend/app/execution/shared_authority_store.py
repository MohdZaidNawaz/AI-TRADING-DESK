"""Fail-closed shared SQLite ownership for local authority lifecycle state.

The private approval lifecycle primitives are used only by the application-owned
trusted Sentinel issuance service. This module has no Pilot, adapter, MT5,
broker, API-route, or submission dependency. Private demo-authorization and
test-only reservation primitives exercise the shared transaction boundary but
remain disconnected from production composition and cannot submit an order.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Callable, Iterator

from app.models.shared_authority import (
    SharedAuthorityBootstrapEvent,
    SharedAuthorityDiagnostics,
    SharedAuthorityEntityKind,
    SharedAuthorityMode,
    SharedAuthorityOutboxEvent,
)
from app.models.demo_authorization import (
    ControlledDemoAuthorization,
    DemoAuthorizationStatus,
)
from app.models.durable_execution import (
    DurableAuthorizationConsumptionState,
    DurableExecutionReservation,
    DurableReservationStatus,
)
from app.models.proposals import SentinelDecision, SentinelDecisionStatus


_SCHEMA_VERSION = "2"
_MIGRATION_STATE_READY = "READY"
_MIGRATION_STATE_MIGRATING = "MIGRATING"
_BOOTSTRAP_ENTITY_ID = "shared-authority-schema-bootstrap"
_BOOTSTRAP_ENTITY_FINGERPRINT = sha256(_BOOTSTRAP_ENTITY_ID.encode()).hexdigest()
_REQUIRED_TABLES = frozenset(
    {
        "shared_authority_schema_metadata",
        "shared_authority_schema_migrations",
        "shared_authority_cutover_state",
        "shared_authority_entities",
        "shared_sentinel_approvals",
        "shared_demo_authorizations",
        "shared_execution_reservations",
        "shared_authority_lifecycle_events",
        "shared_authority_outbox",
    }
)
_REQUIRED_INDEXES = frozenset(
    {
        "shared_authority_lifecycle_entity_version_index",
        "shared_authority_outbox_pending_index",
    }
)


@dataclass(frozen=True)
class _TableStructure:
    """SQLite-introspected representation of one canonical table."""

    sql: str
    columns: tuple[tuple[object, ...], ...]
    foreign_keys: tuple[tuple[object, ...], ...]
    unique_indexes: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _IndexStructure:
    """SQLite-introspected representation of a required named index."""

    sql: str
    columns: tuple[str, ...]
    unique: bool


class SharedAuthorityStoreError(RuntimeError):
    """Base error for the non-authoritative shared-authority bootstrap."""


class SharedAuthorityUnavailable(SharedAuthorityStoreError):
    """The schema is unavailable, invalid, incomplete, or cannot commit safely."""


class SharedAuthorityConflict(SharedAuthorityStoreError):
    """An immutable lifecycle/outbox identity conflicts with stored content."""


class SharedAuthorityProjectionError(SharedAuthorityStoreError):
    """A separate outbox projection failed without changing local delivery state."""


class SharedAuthorityStore:
    """Versioned SQLite bootstrap foundation for a future single authority owner.

    The schema reserves normalized approval, authorization, and reservation
    tables, but Phase 13C-1 exposes no methods that write those authority
    records.  This prevents accidental activation before a reviewed cutover.
    """

    _schema_lock = RLock()

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("shared authority timeout must be between 0 and 30 seconds")
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._available = True
        self._failure_reason: str | None = None
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> SharedAuthorityMode:
        return SharedAuthorityMode.SENTINEL_APPROVAL_OWNER

    def diagnostics(self) -> SharedAuthorityDiagnostics:
        if not self._available:
            return SharedAuthorityDiagnostics(
                available=False,
                schema_version=None,
                migration_state=None,
                reason_code=self._failure_reason or "SHARED_AUTHORITY_UNAVAILABLE",
            )
        try:
            with self._connection() as connection:
                metadata = dict(
                    connection.execute(
                        "SELECT key, value FROM shared_authority_schema_metadata"
                    ).fetchall()
                )
            return SharedAuthorityDiagnostics(
                available=True,
                mode=self.mode,
                schema_version=metadata.get("schema_version"),
                migration_state=metadata.get("migration_state"),
            )
        except SharedAuthorityUnavailable:
            return SharedAuthorityDiagnostics(
                available=False,
                schema_version=None,
                migration_state=None,
                reason_code=self._failure_reason or "SHARED_AUTHORITY_UNAVAILABLE",
            )

    def foreign_keys_enabled(self) -> bool:
        """Diagnostic seam proving every store connection enables foreign keys."""
        with self._connection() as connection:
            row = connection.execute("PRAGMA foreign_keys").fetchone()
            return bool(row and row[0] == 1)

    def recover_non_submitting_lifecycle(
        self, *, now: datetime
    ) -> tuple[tuple[str, str], ...]:
        """Conservatively recover local state without retrying an external action.

        Expired active authority is made terminal. Any reservation that had
        become locally ready or started is made reconciliation-required after
        restart unless it has already expired. The method never infers broker
        state and has no submission or provider dependency.
        """

        now_utc = _aware(now)
        recovered: list[tuple[str, str]] = []
        with self._lock:
            try:
                with self._transaction() as connection:
                    expiring = connection.execute(
                        "SELECT e.entity_id, e.entity_kind, e.status, a.expires_at "
                        "FROM shared_authority_entities e "
                        "JOIN shared_sentinel_approvals a ON a.entity_id = e.entity_id "
                        "WHERE e.status = 'active' "
                        "UNION ALL "
                        "SELECT e.entity_id, e.entity_kind, e.status, a.expires_at "
                        "FROM shared_authority_entities e "
                        "JOIN shared_demo_authorizations a ON a.entity_id = e.entity_id "
                        "WHERE e.status = 'active' "
                        "ORDER BY 1"
                    ).fetchall()
                    for entity_id, kind, status, expires_at in expiring:
                        if now_utc < datetime.fromisoformat(str(expires_at)).astimezone(UTC):
                            continue
                        entity_kind = SharedAuthorityEntityKind(str(kind))
                        self._transition_entity(
                            connection,
                            entity_id=str(entity_id),
                            entity_kind=entity_kind,
                            expected_status=str(status),
                            to_status="expired",
                            event_state=f"{entity_kind.value}_expired_during_recovery",
                            reason_code="SHARED_AUTHORITY_EXPIRED",
                            now=now_utc,
                        )
                        recovered.append((str(entity_id), "expired"))

                    reservations = connection.execute(
                        "SELECT e.entity_id, e.entity_kind, e.status, r.expires_at "
                        "FROM shared_authority_entities e "
                        "JOIN shared_execution_reservations r ON r.entity_id = e.entity_id "
                        "WHERE e.status IN ('reserved', 'ready_for_attempt', 'attempt_started') "
                        "ORDER BY e.entity_id"
                    ).fetchall()
                    for entity_id, kind, status, expires_at in reservations:
                        expired = now_utc >= datetime.fromisoformat(
                            str(expires_at)
                        ).astimezone(UTC)
                        target = "expired" if expired else "reconciliation_required"
                        self._transition_entity(
                            connection,
                            entity_id=str(entity_id),
                            entity_kind=SharedAuthorityEntityKind(str(kind)),
                            expected_status=str(status),
                            to_status=target,
                            event_state=(
                                "test_only_reservation_expired_during_recovery"
                                if expired
                                else "test_only_reservation_reconciliation_required"
                            ),
                            reason_code=(
                                "SHARED_AUTHORITY_EXPIRED"
                                if expired
                                else "EXTERNAL_OUTCOME_NOT_INFERRED"
                            ),
                            now=now_utc,
                        )
                        recovered.append((str(entity_id), target))
                return tuple(recovered)
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
                self._fail_closed("SHARED_AUTHORITY_RECOVERY_UNAVAILABLE")
                raise SharedAuthorityUnavailable(
                    "shared authority recovery unavailable"
                ) from error

    # These underscore-prefixed operations are intentionally not a public
    # issuance API. The application-owned trusted issuance service is their
    # sole production caller and derives decisions from normal proposal,
    # evidence, and risk inputs. Python privacy is not cryptographic isolation
    # from arbitrary in-process code; this is controlled app composition.
    def _persist_sentinel_evaluation(
        self, decision: SentinelDecision, *, now: datetime
    ) -> tuple[SentinelDecision, bool]:
        if decision.decision is not SentinelDecisionStatus.APPROVED or not decision.approval_id:
            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_REQUIRED")
        if decision.expires_at is None or now.tzinfo is None or now.utcoffset() is None:
            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_INVALID")
        now = now.astimezone(UTC)
        if now >= decision.expires_at:
            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_EXPIRED")
        fingerprint = _decision_fingerprint(decision)
        entity_id = "shared-sentinel-approval-" + decision.approval_id
        with self._lock:
            try:
                with self._transaction() as connection:
                    existing = connection.execute(
                        "SELECT semantic_fingerprint FROM shared_authority_entities WHERE entity_id = ?",
                        (entity_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != fingerprint:
                            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_CONFLICT")
                        return decision, False
                    connection.execute(
                        "INSERT INTO shared_authority_entities (entity_id, entity_kind, status, lifecycle_version, semantic_fingerprint, created_at, updated_at) VALUES (?, ?, 'active', 1, ?, ?, ?)",
                        (entity_id, SharedAuthorityEntityKind.SENTINEL_APPROVAL.value, fingerprint, now.isoformat(), now.isoformat()),
                    )
                    connection.execute(
                        "INSERT INTO shared_sentinel_approvals (entity_id, approval_id, decision_id, proposal_id, proposal_fingerprint, analysis_id, evidence_fingerprint, policy_id, policy_fingerprint, assessment_id, assessment_fingerprint, account_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (entity_id, decision.approval_id, decision.decision_id, decision.proposal_id, decision.proposal_fingerprint, decision.analysis_id, decision.evidence_fingerprint, decision.policy_id, decision.policy_fingerprint, decision.assessment_id, decision.assessment_fingerprint, decision.account_id, decision.expires_at.isoformat()),
                    )
                    self._append_sentinel_lifecycle(
                        connection,
                        entity_id=entity_id,
                        approval_id=decision.approval_id,
                        decision_fingerprint=fingerprint,
                        status="active",
                        from_status=None,
                        version=1,
                        event_state="sentinel_approval_issued",
                        now=now,
                    )
                    return decision, True
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
                self._fail_closed("SHARED_SENTINEL_APPROVAL_UNAVAILABLE")
                raise SharedAuthorityUnavailable("shared Sentinel approval unavailable") from error

    def _verify_sentinel_approval(self, decision: SentinelDecision, *, now: datetime) -> None:
        self._sentinel_state(decision, now=now, consume=False)

    def _consume_sentinel_approval(self, decision: SentinelDecision, *, now: datetime) -> None:
        self._sentinel_state(decision, now=now, consume=True)

    def _revoke_sentinel_approval(self, approval_id: str, *, now: datetime) -> None:
        if not approval_id.strip() or now.tzinfo is None or now.utcoffset() is None:
            raise SharedAuthorityConflict("SHARED_SENTINEL_REVOCATION_INVALID")
        entity_id = "shared-sentinel-approval-" + approval_id
        with self._lock:
            try:
                with self._transaction() as connection:
                    row = connection.execute(
                        "SELECT status, lifecycle_version, semantic_fingerprint "
                        "FROM shared_authority_entities WHERE entity_id = ?",
                        (entity_id,),
                    ).fetchone()
                    if row is None or row[0] != "active":
                        raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_UNAVAILABLE")
                    version = int(row[1]) + 1
                    occurred_at = now.astimezone(UTC)
                    connection.execute(
                        "UPDATE shared_authority_entities SET status = 'revoked', "
                        "lifecycle_version = ?, updated_at = ? WHERE entity_id = ?",
                        (version, occurred_at.isoformat(), entity_id),
                    )
                    self._append_sentinel_lifecycle(
                        connection,
                        entity_id=entity_id,
                        approval_id=approval_id,
                        decision_fingerprint=str(row[2]),
                        status="revoked",
                        from_status="active",
                        version=version,
                        event_state="sentinel_approval_revoked",
                        now=occurred_at,
                    )
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
                self._fail_closed("SHARED_SENTINEL_REVOCATION_UNAVAILABLE")
                raise SharedAuthorityUnavailable("shared Sentinel approval unavailable") from error

    def _persist_demo_authorization(
        self,
        authorization: ControlledDemoAuthorization,
        decision: SentinelDecision,
        *,
        now: datetime,
    ) -> tuple[ControlledDemoAuthorization, bool]:
        """Persist an application-derived demo authorization without consuming it.

        This is an internal persistence seam, not an issuance API. The caller
        must be the existing authorization service after it has completed its
        policy, trusted-evidence, readiness, kill-switch, and audit checks.
        """

        now_utc = _aware(now)
        self._validate_demo_binding(authorization, decision, now=now_utc)
        entity_id = _demo_entity_id(authorization.authorization_id)
        fingerprint = authorization.fingerprint
        with self._lock:
            try:
                with self._transaction() as connection:
                    approval_entity_id = self._require_active_approval(
                        connection, decision, now=now_utc
                    )
                    existing = connection.execute(
                        "SELECT semantic_fingerprint FROM shared_authority_entities "
                        "WHERE entity_id = ?",
                        (entity_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != fingerprint:
                            raise SharedAuthorityConflict(
                                "SHARED_DEMO_AUTHORIZATION_CONFLICT"
                            )
                        return authorization, False
                    connection.execute(
                        "INSERT INTO shared_authority_entities "
                        "(entity_id, entity_kind, status, lifecycle_version, "
                        "semantic_fingerprint, created_at, updated_at) "
                        "VALUES (?, ?, 'active', 1, ?, ?, ?)",
                        (
                            entity_id,
                            SharedAuthorityEntityKind.DEMO_AUTHORIZATION.value,
                            fingerprint,
                            now_utc.isoformat(),
                            now_utc.isoformat(),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO shared_demo_authorizations "
                        "(entity_id, authorization_id, approval_entity_id, "
                        "execution_intent_id, execution_intent_fingerprint, "
                        "proposal_fingerprint, evidence_fingerprint, policy_id, "
                        "policy_fingerprint, account_id, account_reference, "
                        "broker_server, canonical_symbol, broker_symbol, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entity_id,
                            authorization.authorization_id,
                            approval_entity_id,
                            authorization.execution_intent_id,
                            authorization.execution_intent_fingerprint,
                            authorization.proposal_fingerprint,
                            authorization.evidence_fingerprint,
                            authorization.policy_id,
                            authorization.policy_fingerprint,
                            authorization.account.account_id,
                            authorization.account.source_account_id,
                            authorization.account.account_reference,
                            authorization.instrument.canonical_symbol,
                            authorization.instrument.broker_symbol,
                            authorization.expires_at.isoformat(),
                        ),
                    )
                    self._append_entity_lifecycle(
                        connection,
                        entity_id=entity_id,
                        entity_kind=SharedAuthorityEntityKind.DEMO_AUTHORIZATION,
                        semantic_fingerprint=fingerprint,
                        from_status=None,
                        to_status="active",
                        version=1,
                        event_state="demo_authorization_persisted",
                        reason_code="NON_SUBMITTING_AUTHORIZATION_LIFECYCLE",
                        now=now_utc,
                    )
                    return authorization, True
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
                self._fail_closed("SHARED_DEMO_AUTHORIZATION_UNAVAILABLE")
                raise SharedAuthorityUnavailable(
                    "shared demo authorization unavailable"
                ) from error

    def _verify_demo_authorization(
        self, authorization: ControlledDemoAuthorization, *, now: datetime
    ) -> None:
        """Read-only exact-lineage verification of one stored authorization."""

        now_utc = _aware(now)
        if authorization.status is not DemoAuthorizationStatus.AUTHORIZED:
            raise SharedAuthorityConflict("SHARED_DEMO_AUTHORIZATION_INVALID")
        entity_id = _demo_entity_id(authorization.authorization_id)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, semantic_fingerprint FROM shared_authority_entities "
                "WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
            if row is None or row[1] != authorization.fingerprint:
                raise SharedAuthorityConflict(
                    "SHARED_DEMO_AUTHORIZATION_LINEAGE_MISMATCH"
                )
            if row[0] != "active":
                raise SharedAuthorityConflict(
                    "SHARED_DEMO_AUTHORIZATION_" + str(row[0]).upper()
                )
            if now_utc >= authorization.expires_at:
                raise SharedAuthorityConflict("SHARED_DEMO_AUTHORIZATION_EXPIRED")

    def _commit_test_only_reservation(
        self,
        reservation: DurableExecutionReservation,
        *,
        decision: SentinelDecision,
        authorization: ControlledDemoAuthorization,
        now: datetime,
    ) -> tuple[DurableExecutionReservation, bool]:
        """Atomically consume local authority and create a test-only reservation.

        The resulting record is deliberately non-submitting. It cannot be used
        as production authority and this store has no adapter or broker path.
        The method exists to make the local SQLite transaction boundary real
        and testable without claiming atomicity with any external operation.
        """

        now_utc = _aware(now)
        self._validate_reservation_binding(
            reservation,
            decision=decision,
            authorization=authorization,
            now=now_utc,
        )
        committed = reservation.model_copy(
            update={
                "status": DurableReservationStatus.READY_FOR_ATTEMPT,
                "authorization_consumption": (
                    DurableAuthorizationConsumptionState.CONSUMED_CONFIRMED
                ),
                "lifecycle_version": 1,
            }
        )
        entity_id = _reservation_entity_id(committed.reservation_id)
        with self._lock:
            try:
                with self._transaction() as connection:
                    existing = connection.execute(
                        "SELECT status, semantic_fingerprint FROM shared_authority_entities "
                        "WHERE entity_id = ?",
                        (entity_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing[1] != committed.fingerprint:
                            raise SharedAuthorityConflict(
                                "SHARED_EXECUTION_RESERVATION_CONFLICT"
                            )
                        if existing[0] != "ready_for_attempt":
                            raise SharedAuthorityConflict(
                                "SHARED_EXECUTION_RESERVATION_"
                                + str(existing[0]).upper()
                            )
                        return committed, False

                    approval_entity_id = self._require_active_approval(
                        connection, decision, now=now_utc
                    )
                    authorization_entity_id = self._require_active_authorization(
                        connection, authorization, now=now_utc
                    )
                    connection.execute(
                        "INSERT INTO shared_authority_entities "
                        "(entity_id, entity_kind, status, lifecycle_version, "
                        "semantic_fingerprint, created_at, updated_at) "
                        "VALUES (?, ?, 'ready_for_attempt', 1, ?, ?, ?)",
                        (
                            entity_id,
                            SharedAuthorityEntityKind.EXECUTION_RESERVATION.value,
                            committed.fingerprint,
                            now_utc.isoformat(),
                            now_utc.isoformat(),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO shared_execution_reservations "
                        "(entity_id, reservation_id, execution_identity, "
                        "approval_entity_id, authorization_entity_id, "
                        "safety_evidence_fingerprint, proposal_fingerprint, "
                        "evidence_fingerprint, execution_intent_fingerprint, "
                        "account_id, account_reference, broker_server, "
                        "canonical_symbol, broker_symbol, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entity_id,
                            committed.reservation_id,
                            committed.execution_identity,
                            approval_entity_id,
                            authorization_entity_id,
                            committed.safety_evidence.fingerprint,
                            committed.proposal_fingerprint,
                            committed.evidence_fingerprint,
                            committed.execution_intent_fingerprint,
                            committed.account_id,
                            committed.account_reference,
                            committed.broker_server,
                            committed.canonical_symbol,
                            committed.broker_symbol,
                            committed.expires_at.isoformat(),
                        ),
                    )
                    self._transition_entity(
                        connection,
                        entity_id=approval_entity_id,
                        entity_kind=SharedAuthorityEntityKind.SENTINEL_APPROVAL,
                        expected_status="active",
                        to_status="consumed",
                        event_state="sentinel_approval_consumed_for_test_reservation",
                        reason_code="NON_SUBMITTING_TEST_ONLY_RESERVATION",
                        now=now_utc,
                    )
                    self._transition_entity(
                        connection,
                        entity_id=authorization_entity_id,
                        entity_kind=SharedAuthorityEntityKind.DEMO_AUTHORIZATION,
                        expected_status="active",
                        to_status="consumed",
                        event_state="demo_authorization_consumed_for_test_reservation",
                        reason_code="NON_SUBMITTING_TEST_ONLY_RESERVATION",
                        now=now_utc,
                    )
                    self._append_entity_lifecycle(
                        connection,
                        entity_id=entity_id,
                        entity_kind=SharedAuthorityEntityKind.EXECUTION_RESERVATION,
                        semantic_fingerprint=committed.fingerprint,
                        from_status=None,
                        to_status="ready_for_attempt",
                        version=1,
                        event_state="test_only_reservation_committed",
                        reason_code="NON_SUBMITTING_TEST_ONLY_RESERVATION",
                        now=now_utc,
                    )
                    return committed, True
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
                self._fail_closed("SHARED_EXECUTION_RESERVATION_UNAVAILABLE")
                raise SharedAuthorityUnavailable(
                    "shared execution reservation unavailable"
                ) from error

    @staticmethod
    def _validate_demo_binding(
        authorization: ControlledDemoAuthorization,
        decision: SentinelDecision,
        *,
        now: datetime,
    ) -> None:
        if (
            decision.decision is not SentinelDecisionStatus.APPROVED
            or not decision.approval_id
            or decision.expires_at is None
            or authorization.status is not DemoAuthorizationStatus.AUTHORIZED
        ):
            raise SharedAuthorityConflict("SHARED_DEMO_AUTHORIZATION_INVALID")
        if now >= min(decision.expires_at, authorization.expires_at):
            raise SharedAuthorityConflict("SHARED_DEMO_AUTHORIZATION_EXPIRED")
        expected = (
            decision.approval_id,
            decision.proposal_id,
            decision.proposal_fingerprint,
            decision.analysis_id,
            decision.evidence_fingerprint,
            decision.policy_id,
            decision.policy_version,
            decision.policy_fingerprint,
            decision.account_id,
        )
        actual = (
            authorization.approval_id,
            authorization.proposal_id,
            authorization.proposal_fingerprint,
            authorization.analysis_id,
            authorization.evidence_fingerprint,
            authorization.policy_id,
            authorization.policy_version,
            authorization.policy_fingerprint,
            authorization.account.account_id,
        )
        if actual != expected or authorization.expires_at > decision.expires_at:
            raise SharedAuthorityConflict(
                "SHARED_DEMO_AUTHORIZATION_LINEAGE_MISMATCH"
            )

    @classmethod
    def _validate_reservation_binding(
        cls,
        reservation: DurableExecutionReservation,
        *,
        decision: SentinelDecision,
        authorization: ControlledDemoAuthorization,
        now: datetime,
    ) -> None:
        cls._validate_demo_binding(authorization, decision, now=now)
        try:
            reconstructed = DurableExecutionReservation.model_validate(
                reservation.model_dump(
                    mode="python", exclude_computed_fields=True
                )
            )
        except (TypeError, ValueError) as error:
            raise SharedAuthorityConflict(
                "SHARED_EXECUTION_RESERVATION_INVALID"
            ) from error
        if reconstructed != reservation:
            raise SharedAuthorityConflict("SHARED_EXECUTION_RESERVATION_INVALID")
        if (
            reservation.status is not DurableReservationStatus.RESERVED
            or reservation.authorization_consumption
            is not DurableAuthorizationConsumptionState.UNCONSUMED
            or not reservation.test_only
            or now >= reservation.expires_at
        ):
            raise SharedAuthorityConflict("SHARED_EXECUTION_RESERVATION_INVALID")
        approval_fingerprint = _decision_fingerprint(decision)
        expected = (
            decision.approval_id,
            approval_fingerprint,
            decision.policy_id,
            decision.policy_version,
            decision.proposal_id,
            decision.proposal_fingerprint,
            decision.analysis_id,
            decision.evidence_fingerprint,
            authorization.authorization_id,
            authorization.fingerprint,
            authorization.execution_intent_id,
            authorization.execution_intent_fingerprint,
            authorization.account.account_id,
            authorization.account.source_account_id,
            authorization.account.account_reference,
            authorization.instrument.canonical_symbol,
            authorization.instrument.broker_symbol,
            Decimal(str(authorization.quantity)),
            authorization.order_type,
            authorization.filling_mode,
        )
        actual = (
            reservation.sentinel_approval_id,
            reservation.sentinel_approval_fingerprint,
            reservation.sentinel_policy_id,
            reservation.policy_version,
            reservation.proposal_id,
            reservation.proposal_fingerprint,
            reservation.analysis_id,
            reservation.evidence_fingerprint,
            reservation.controlled_demo_authorization_id,
            reservation.controlled_demo_authorization_fingerprint,
            reservation.execution_intent_id,
            reservation.execution_intent_fingerprint,
            reservation.account_id,
            reservation.account_reference,
            reservation.broker_server,
            reservation.canonical_symbol,
            reservation.broker_symbol,
            reservation.quantity,
            reservation.order_type,
            reservation.filling_mode,
        )
        safety = reservation.safety_evidence
        if (
            safety.market_reference.observed_at > now
            or (now - safety.market_reference.observed_at).total_seconds()
            > safety.market_reference.max_age_seconds
            or safety.broker_instrument.observed_at > now
            or safety.portfolio_risk.observed_at > now
            or now >= safety.broker_instrument.expires_at
            or now >= safety.portfolio_risk.expires_at
            or now >= safety.sentinel_approval.expires_at
            or now >= safety.demo_authorization.expires_at
            or safety.portfolio_risk.risk_policy_fingerprint
            != decision.policy_fingerprint
        ):
            raise SharedAuthorityConflict(
                "SHARED_EXECUTION_RESERVATION_EVIDENCE_UNAVAILABLE"
            )
        nested_approval = safety.sentinel_approval
        nested_authorization = safety.demo_authorization
        nested_expected = (
            decision.decision_id,
            decision.approval_id,
            approval_fingerprint,
            decision.proposal_id,
            decision.proposal_fingerprint,
            decision.analysis_id,
            decision.evidence_fingerprint,
            decision.policy_id,
            decision.policy_version,
            decision.policy_fingerprint,
            decision.assessment_id,
            decision.assessment_fingerprint,
            decision.account_id,
            authorization.authorization_id,
            authorization.fingerprint,
            authorization.policy_id,
            authorization.policy_fingerprint,
            authorization.account.account_id,
            authorization.account.source_account_id,
            authorization.account.account_reference,
            authorization.proposal_id,
            authorization.proposal_fingerprint,
            authorization.analysis_id,
            authorization.evidence_fingerprint,
            authorization.execution_intent_id,
            authorization.execution_intent_fingerprint,
            authorization.instrument.canonical_symbol,
            authorization.instrument.broker_symbol,
        )
        nested_actual = (
            nested_approval.decision_id,
            nested_approval.approval_id,
            nested_approval.approval_fingerprint,
            nested_approval.proposal_id,
            nested_approval.proposal_fingerprint,
            nested_approval.analysis_id,
            nested_approval.evidence_fingerprint,
            nested_approval.policy_id,
            nested_approval.policy_version,
            nested_approval.policy_fingerprint,
            nested_approval.assessment_id,
            nested_approval.assessment_fingerprint,
            nested_approval.account_id,
            nested_authorization.authorization_id,
            nested_authorization.authorization_fingerprint,
            nested_authorization.policy_id,
            nested_authorization.policy_fingerprint,
            nested_authorization.account_id,
            nested_authorization.account_reference,
            nested_authorization.broker_server,
            nested_authorization.proposal_id,
            nested_authorization.proposal_fingerprint,
            nested_authorization.analysis_id,
            nested_authorization.evidence_fingerprint,
            nested_authorization.execution_intent_id,
            nested_authorization.execution_intent_fingerprint,
            nested_authorization.canonical_symbol,
            nested_authorization.broker_symbol,
        )
        if actual != expected or nested_actual != nested_expected:
            raise SharedAuthorityConflict(
                "SHARED_EXECUTION_RESERVATION_LINEAGE_MISMATCH"
            )
        if reservation.expires_at > min(
            decision.expires_at,
            authorization.expires_at,
            nested_approval.expires_at,
            nested_authorization.expires_at,
            safety.broker_instrument.expires_at,
            safety.portfolio_risk.expires_at,
        ):
            raise SharedAuthorityConflict(
                "SHARED_EXECUTION_RESERVATION_EXPIRY_MISMATCH"
            )

    @staticmethod
    def _require_active_approval(
        connection: sqlite3.Connection,
        decision: SentinelDecision,
        *,
        now: datetime,
    ) -> str:
        if not decision.approval_id or decision.expires_at is None:
            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_INVALID")
        entity_id = _approval_entity_id(decision.approval_id)
        row = connection.execute(
            "SELECT status, semantic_fingerprint FROM shared_authority_entities "
            "WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if row is None or row[1] != _decision_fingerprint(decision):
            raise SharedAuthorityConflict(
                "SHARED_SENTINEL_APPROVAL_LINEAGE_MISMATCH"
            )
        if row[0] != "active":
            raise SharedAuthorityConflict(
                "SHARED_SENTINEL_APPROVAL_" + str(row[0]).upper()
            )
        if now >= decision.expires_at:
            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_EXPIRED")
        return entity_id

    @staticmethod
    def _require_active_authorization(
        connection: sqlite3.Connection,
        authorization: ControlledDemoAuthorization,
        *,
        now: datetime,
    ) -> str:
        entity_id = _demo_entity_id(authorization.authorization_id)
        row = connection.execute(
            "SELECT status, semantic_fingerprint FROM shared_authority_entities "
            "WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if row is None or row[1] != authorization.fingerprint:
            raise SharedAuthorityConflict(
                "SHARED_DEMO_AUTHORIZATION_LINEAGE_MISMATCH"
            )
        if row[0] != "active":
            raise SharedAuthorityConflict(
                "SHARED_DEMO_AUTHORIZATION_" + str(row[0]).upper()
            )
        if now >= authorization.expires_at:
            raise SharedAuthorityConflict("SHARED_DEMO_AUTHORIZATION_EXPIRED")
        return entity_id

    def _transition_entity(
        self,
        connection: sqlite3.Connection,
        *,
        entity_id: str,
        entity_kind: SharedAuthorityEntityKind,
        expected_status: str,
        to_status: str,
        event_state: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT status, lifecycle_version, semantic_fingerprint "
            "FROM shared_authority_entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if row is None or row[0] != expected_status:
            raise SharedAuthorityConflict("SHARED_AUTHORITY_TRANSITION_CONFLICT")
        version = int(row[1]) + 1
        updated = connection.execute(
            "UPDATE shared_authority_entities SET status = ?, lifecycle_version = ?, "
            "updated_at = ? WHERE entity_id = ? AND status = ? AND lifecycle_version = ?",
            (
                to_status,
                version,
                now.isoformat(),
                entity_id,
                expected_status,
                int(row[1]),
            ),
        )
        if updated.rowcount != 1:
            raise SharedAuthorityConflict("SHARED_AUTHORITY_TRANSITION_CONFLICT")
        self._append_entity_lifecycle(
            connection,
            entity_id=entity_id,
            entity_kind=entity_kind,
            semantic_fingerprint=str(row[2]),
            from_status=expected_status,
            to_status=to_status,
            version=version,
            event_state=event_state,
            reason_code=reason_code,
            now=now,
        )

    def _append_entity_lifecycle(
        self,
        connection: sqlite3.Connection,
        *,
        entity_id: str,
        entity_kind: SharedAuthorityEntityKind,
        semantic_fingerprint: str,
        from_status: str | None,
        to_status: str,
        version: int,
        event_state: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        fingerprint = _fingerprint(
            {
                "entity_id": entity_id,
                "entity_kind": entity_kind.value,
                "semantic_fingerprint": semantic_fingerprint,
                "from_status": from_status,
                "to_status": to_status,
                "version": version,
                "event_state": event_state,
                "reason_code": reason_code,
            }
        )
        event_id = "shared-authority-event-" + fingerprint
        connection.execute(
            "INSERT INTO shared_authority_lifecycle_events "
            "(event_id, entity_id, lifecycle_version, from_status, to_status, "
            "event_state, reason_code, occurred_at, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                entity_id,
                version,
                from_status,
                to_status,
                event_state,
                reason_code,
                now.isoformat(),
                fingerprint,
            ),
        )
        outbox_id = "shared-authority-outbox-" + sha256(
            f"{event_id}:{version}".encode()
        ).hexdigest()
        connection.execute(
            "INSERT INTO shared_authority_outbox "
            "(outbox_id, lifecycle_event_id, entity_id, lifecycle_version, "
            "event_state, reason_code, occurred_at, fingerprint, delivered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                outbox_id,
                event_id,
                entity_id,
                version,
                event_state,
                reason_code,
                now.isoformat(),
                fingerprint,
            ),
        )

    def _sentinel_state(self, decision: SentinelDecision, *, now: datetime, consume: bool) -> None:
        if not decision.approval_id or now.tzinfo is None or now.utcoffset() is None:
            raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_INVALID")
        now = now.astimezone(UTC)
        entity_id = "shared-sentinel-approval-" + decision.approval_id
        terminal_error: str | None = None
        with self._lock:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT status, lifecycle_version, semantic_fingerprint "
                    "FROM shared_authority_entities WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
                if row is None or row[2] != _decision_fingerprint(decision):
                    raise SharedAuthorityConflict("SHARED_SENTINEL_APPROVAL_LINEAGE_MISMATCH")
                if row[0] != "active":
                    raise SharedAuthorityConflict(
                        "SHARED_SENTINEL_APPROVAL_" + str(row[0]).upper()
                    )
                fingerprint = _decision_fingerprint(decision)
                if decision.expires_at is None or now >= decision.expires_at:
                    version = int(row[1]) + 1
                    connection.execute("UPDATE shared_authority_entities SET status = 'expired', lifecycle_version = ?, updated_at = ? WHERE entity_id = ?", (version, now.isoformat(), entity_id))
                    self._append_sentinel_lifecycle(
                        connection,
                        entity_id=entity_id,
                        approval_id=decision.approval_id,
                        decision_fingerprint=fingerprint,
                        status="expired",
                        from_status="active",
                        version=version,
                        event_state="sentinel_approval_expired",
                        now=now,
                    )
                    # Commit the expiry transition and its outbox record before
                    # reporting the terminal result to the caller.
                    terminal_error = "SHARED_SENTINEL_APPROVAL_EXPIRED"
                elif consume:
                    version = int(row[1]) + 1
                    connection.execute("UPDATE shared_authority_entities SET status = 'consumed', lifecycle_version = ?, updated_at = ? WHERE entity_id = ?", (version, now.isoformat(), entity_id))
                    self._append_sentinel_lifecycle(
                        connection,
                        entity_id=entity_id,
                        approval_id=decision.approval_id,
                        decision_fingerprint=fingerprint,
                        status="consumed",
                        from_status="active",
                        version=version,
                        event_state="sentinel_approval_consumed",
                        now=now,
                    )
        if terminal_error is not None:
            raise SharedAuthorityConflict(terminal_error)

    def _append_sentinel_lifecycle(
        self,
        connection: sqlite3.Connection,
        *,
        entity_id: str,
        approval_id: str,
        decision_fingerprint: str,
        status: str,
        from_status: str | None,
        version: int,
        event_state: str,
        now: datetime,
    ) -> None:
        fingerprint = _fingerprint(
            {
                "approval_id": approval_id,
                "status": status,
                "version": version,
                "decision_fingerprint": decision_fingerprint,
            }
        )
        event_id = "shared-sentinel-event-" + fingerprint
        connection.execute("INSERT INTO shared_authority_lifecycle_events (event_id, entity_id, lifecycle_version, from_status, to_status, event_state, reason_code, occurred_at, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (event_id, entity_id, version, from_status, status, event_state, "SENTINEL_DURABLE_AUTHORITY", now.isoformat(), fingerprint))
        outbox_id = "shared-authority-outbox-" + sha256(f"{event_id}:{version}".encode()).hexdigest()
        connection.execute("INSERT INTO shared_authority_outbox (outbox_id, lifecycle_event_id, entity_id, lifecycle_version, event_state, reason_code, occurred_at, fingerprint, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)", (outbox_id, event_id, entity_id, version, event_state, "SENTINEL_DURABLE_AUTHORITY", now.isoformat(), fingerprint))

    def record_bootstrap_lifecycle(
        self, event: SharedAuthorityBootstrapEvent
    ) -> tuple[SharedAuthorityOutboxEvent, bool]:
        """Atomically append one non-authoritative schema-bootstrap lifecycle event.

        This is intentionally the only mutation primitive in Phase 13C-1.  It
        demonstrates lifecycle/outbox atomicity without creating any approval,
        authorization, reservation, or execution authority.
        """
        with self._lock:
            try:
                with self._transaction() as connection:
                    self._ensure_bootstrap_entity(connection, event.occurred_at)
                    existing = connection.execute(
                        "SELECT fingerprint FROM shared_authority_lifecycle_events WHERE event_id = ?",
                        (event.event_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != event.fingerprint:
                            raise SharedAuthorityConflict("SHARED_AUTHORITY_LIFECYCLE_CONFLICT")
                        return self._outbox_by_lifecycle_id(connection, event.event_id), False

                    version = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(lifecycle_version), 0) + 1 "
                            "FROM shared_authority_lifecycle_events WHERE entity_id = ?",
                            (_BOOTSTRAP_ENTITY_ID,),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        "UPDATE shared_authority_entities "
                        "SET lifecycle_version = ?, updated_at = ? WHERE entity_id = ?",
                        (version, event.occurred_at.isoformat(), _BOOTSTRAP_ENTITY_ID),
                    )
                    connection.execute(
                        """INSERT INTO shared_authority_lifecycle_events (
                            event_id, entity_id, lifecycle_version, from_status,
                            to_status, event_state, reason_code, occurred_at, fingerprint
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event.event_id,
                            _BOOTSTRAP_ENTITY_ID,
                            version,
                            "bootstrap",
                            "bootstrap",
                            event.event_state,
                            event.reason_code,
                            event.occurred_at.isoformat(),
                            event.fingerprint,
                        ),
                    )
                    outbox = self._build_outbox(event, version)
                    connection.execute(
                        """INSERT INTO shared_authority_outbox (
                            outbox_id, lifecycle_event_id, entity_id, lifecycle_version,
                            event_state, reason_code, occurred_at, fingerprint, delivered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                        (
                            outbox.outbox_id,
                            outbox.lifecycle_event_id,
                            outbox.entity_id,
                            outbox.lifecycle_version,
                            outbox.event_state,
                            outbox.reason_code,
                            outbox.occurred_at.isoformat(),
                            outbox.fingerprint,
                        ),
                    )
                    return outbox, True
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
                self._fail_closed("SHARED_AUTHORITY_TRANSACTION_UNAVAILABLE")
                raise SharedAuthorityUnavailable("shared authority transaction unavailable") from error

    def lifecycle_events(self) -> tuple[SharedAuthorityOutboxEvent, ...]:
        """Return immutable bootstrap event records in deterministic order."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """SELECT o.outbox_id, o.lifecycle_event_id, e.entity_kind, o.entity_id,
                    o.lifecycle_version, o.event_state, o.reason_code, o.occurred_at, o.fingerprint
                    FROM shared_authority_outbox AS o
                    JOIN shared_authority_entities AS e ON e.entity_id = o.entity_id
                    ORDER BY o.lifecycle_version, o.outbox_id"""
                ).fetchall()
                return tuple(self._outbox_from_row(row) for row in rows)
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            self._fail_closed("SHARED_AUTHORITY_READ_UNAVAILABLE")
            raise SharedAuthorityUnavailable("shared authority read unavailable") from error

    def pending_outbox(self) -> tuple[SharedAuthorityOutboxEvent, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """SELECT o.outbox_id, o.lifecycle_event_id, e.entity_kind, o.entity_id,
                    o.lifecycle_version, o.event_state, o.reason_code, o.occurred_at, o.fingerprint
                    FROM shared_authority_outbox AS o
                    JOIN shared_authority_entities AS e ON e.entity_id = o.entity_id
                    WHERE o.delivered_at IS NULL
                    ORDER BY o.lifecycle_version, o.outbox_id"""
                ).fetchall()
                return tuple(self._outbox_from_row(row) for row in rows)
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            self._fail_closed("SHARED_AUTHORITY_OUTBOX_UNAVAILABLE")
            raise SharedAuthorityUnavailable("shared authority outbox unavailable") from error

    def project_outbox(
        self, projector: Callable[[SharedAuthorityOutboxEvent], None]
    ) -> int:
        """Project local outbox events idempotently to a separate caller-owned sink.

        The callback is intentionally generic and does not import the audit
        ledger. Delivery acknowledgement is a separate local transaction, so
        this is at-least-once external delivery: if a projector succeeds but
        local acknowledgement fails, replay invokes it again. Projectors must
        deduplicate using the stable ``outbox_id`` when duplicate effects are
        unacceptable. This method never claims cross-database atomicity.
        """
        delivered = 0
        for event in self.pending_outbox():
            try:
                projector(event)
            except Exception as error:  # projector is external to this store
                raise SharedAuthorityProjectionError(
                    "shared authority outbox projection failed"
                ) from error
            try:
                with self._lock:
                    with self._transaction() as connection:
                        changed = connection.execute(
                            "UPDATE shared_authority_outbox SET delivered_at = ? "
                            "WHERE outbox_id = ? AND delivered_at IS NULL",
                            (event.occurred_at.isoformat(), event.outbox_id),
                        ).rowcount
                        delivered += int(changed == 1)
            except SharedAuthorityStoreError:
                raise
            except (sqlite3.DatabaseError, OSError) as error:
                self._fail_closed("SHARED_AUTHORITY_OUTBOX_ACK_UNAVAILABLE")
                raise SharedAuthorityUnavailable(
                    "shared authority outbox acknowledgement unavailable"
                ) from error
        return delivered

    def _initialize(self) -> None:
        try:
            with self._schema_lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                connection = self._open_connection()
                try:
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                        ).fetchall()
                    }
                    if not tables:
                        self._apply_initial_schema(connection)
                    else:
                        metadata = dict(
                            connection.execute(
                                "SELECT key, value FROM shared_authority_schema_metadata"
                            ).fetchall()
                        )
                        if metadata.get("schema_version") == "1":
                            self._verify_existing_schema(connection, tables, schema_version="1")
                            self._apply_v2_migration(connection)
                        else:
                            self._verify_existing_schema(connection, tables)
                finally:
                    connection.close()
        except SharedAuthorityStoreError:
            self._fail_closed("SHARED_AUTHORITY_SCHEMA_INVALID")
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            self._fail_closed("SHARED_AUTHORITY_SCHEMA_UNAVAILABLE")
            raise SharedAuthorityUnavailable("shared authority schema unavailable") from error

    def _apply_initial_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._create_schema_tables(connection)
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO shared_authority_schema_metadata (key, value) VALUES (?, ?)",
                ("schema_version", _SCHEMA_VERSION),
            )
            connection.execute(
                "INSERT INTO shared_authority_schema_metadata (key, value) VALUES (?, ?)",
                ("migration_state", _MIGRATION_STATE_MIGRATING),
            )
            connection.execute(
                "INSERT INTO shared_authority_schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (1, "shared_authority_bootstrap", _migration_checksum(1), now),
            )
            connection.execute(
                "INSERT INTO shared_authority_schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (2, "sentinel_approval_ownership", _migration_checksum(2), now),
            )
            connection.execute(
                "INSERT INTO shared_authority_cutover_state "
                "(singleton_id, state, updated_at) VALUES (1, ?, ?)",
                (SharedAuthorityMode.SENTINEL_APPROVAL_OWNER.value, now),
            )
            connection.execute(
                "UPDATE shared_authority_schema_metadata SET value = ? WHERE key = 'migration_state'",
                (_MIGRATION_STATE_READY,),
            )
            self._verify_existing_schema(connection, _table_names(connection))
            self._commit(connection)
        except Exception:
            connection.rollback()
            raise

    def _apply_v2_migration(self, connection: sqlite3.Connection) -> None:
        """Atomically cut over a valid v1 bootstrap database to v2 ownership."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "UPDATE shared_authority_schema_metadata SET value = ? "
                "WHERE key = 'migration_state'",
                (_MIGRATION_STATE_MIGRATING,),
            )
            connection.execute(
                "ALTER TABLE shared_authority_cutover_state RENAME TO "
                "shared_authority_cutover_state_v1"
            )
            connection.execute(_canonical_table_sql("2")[2])
            connection.execute(
                "INSERT INTO shared_authority_cutover_state (singleton_id, state, updated_at) "
                "VALUES (1, ?, ?)",
                (SharedAuthorityMode.SENTINEL_APPROVAL_OWNER.value, now),
            )
            connection.execute("DROP TABLE shared_authority_cutover_state_v1")
            connection.execute(
                "INSERT INTO shared_authority_schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (2, "sentinel_approval_ownership", _migration_checksum(2), now),
            )
            connection.execute(
                "UPDATE shared_authority_schema_metadata SET value = ? WHERE key = 'schema_version'",
                (_SCHEMA_VERSION,),
            )
            connection.execute(
                "UPDATE shared_authority_schema_metadata SET value = ? WHERE key = 'migration_state'",
                (_MIGRATION_STATE_READY,),
            )
            self._verify_existing_schema(connection, _table_names(connection))
            self._commit(connection)
        except Exception:
            connection.rollback()
            raise

    def _verify_existing_schema(
        self, connection: sqlite3.Connection, tables: set[str], *, schema_version: str | None = None
    ) -> None:
        expected_version = schema_version or _SCHEMA_VERSION
        if not _REQUIRED_TABLES.issubset(tables):
            raise SharedAuthorityUnavailable("shared authority schema is missing or partially migrated")
        if not _REQUIRED_INDEXES.issubset(_index_names(connection)):
            raise SharedAuthorityUnavailable("shared authority schema is missing required indexes")
        metadata = dict(
            connection.execute("SELECT key, value FROM shared_authority_schema_metadata").fetchall()
        )
        if metadata.get("schema_version") != expected_version:
            raise SharedAuthorityUnavailable("unsupported shared authority schema version")
        if metadata.get("migration_state") != _MIGRATION_STATE_READY:
            raise SharedAuthorityUnavailable("shared authority migration is incomplete")
        migration = connection.execute(
            "SELECT name, checksum FROM shared_authority_schema_migrations WHERE version = 1"
        ).fetchone()
        if migration != ("shared_authority_bootstrap", _migration_checksum(1)):
            raise SharedAuthorityUnavailable("shared authority migration history is invalid")
        if expected_version == "2":
            migration_v2 = connection.execute(
                "SELECT name, checksum FROM shared_authority_schema_migrations WHERE version = 2"
            ).fetchone()
            if migration_v2 != ("sentinel_approval_ownership", _migration_checksum(2)):
                raise SharedAuthorityUnavailable("shared authority migration history is invalid")
        cutover = connection.execute(
            "SELECT state FROM shared_authority_cutover_state WHERE singleton_id = 1"
        ).fetchone()
        expected_mode = (
            SharedAuthorityMode.BOOTSTRAP_NON_AUTHORITATIVE
            if expected_version == "1"
            else SharedAuthorityMode.SENTINEL_APPROVAL_OWNER
        )
        if cutover != (expected_mode.value,):
            raise SharedAuthorityUnavailable("shared authority cutover state is invalid")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise SharedAuthorityUnavailable("shared authority database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SharedAuthorityUnavailable("shared authority foreign-key integrity check failed")

        expected_tables, expected_indexes = _canonical_schema_structure(expected_version)
        actual_tables = {
            table_name: _table_structure(connection, table_name)
            for table_name in _REQUIRED_TABLES
        }
        actual_indexes = {
            index_name: _index_structure(connection, index_name)
            for index_name in _REQUIRED_INDEXES
        }
        if actual_tables != expected_tables or actual_indexes != expected_indexes:
            raise SharedAuthorityUnavailable("shared authority schema structure is incompatible")

    def _create_schema_tables(self, connection: sqlite3.Connection) -> None:
        _create_canonical_schema(connection)

    def _ensure_bootstrap_entity(
        self, connection: sqlite3.Connection, occurred_at: datetime
    ) -> None:
        existing = connection.execute(
            "SELECT entity_kind, semantic_fingerprint FROM shared_authority_entities WHERE entity_id = ?",
            (_BOOTSTRAP_ENTITY_ID,),
        ).fetchone()
        if existing is not None:
            if existing != (SharedAuthorityEntityKind.SCHEMA_BOOTSTRAP.value, _BOOTSTRAP_ENTITY_FINGERPRINT):
                raise SharedAuthorityConflict("SHARED_AUTHORITY_BOOTSTRAP_ENTITY_CONFLICT")
            return
        connection.execute(
            """INSERT INTO shared_authority_entities (
                entity_id, entity_kind, status, lifecycle_version, semantic_fingerprint,
                created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (
                _BOOTSTRAP_ENTITY_ID,
                SharedAuthorityEntityKind.SCHEMA_BOOTSTRAP.value,
                "bootstrap",
                _BOOTSTRAP_ENTITY_FINGERPRINT,
                occurred_at.isoformat(),
                occurred_at.isoformat(),
            ),
        )

    def _build_outbox(
        self, event: SharedAuthorityBootstrapEvent, version: int
    ) -> SharedAuthorityOutboxEvent:
        outbox_id = "shared-authority-outbox-" + sha256(
            f"{event.event_id}:{version}".encode()
        ).hexdigest()
        return SharedAuthorityOutboxEvent(
            outbox_id=outbox_id,
            lifecycle_event_id=event.event_id,
            entity_kind=SharedAuthorityEntityKind.SCHEMA_BOOTSTRAP,
            entity_id=_BOOTSTRAP_ENTITY_ID,
            lifecycle_version=version,
            event_state=event.event_state,
            reason_code=event.reason_code,
            occurred_at=event.occurred_at,
            fingerprint=event.fingerprint,
        )

    def _outbox_by_lifecycle_id(
        self, connection: sqlite3.Connection, event_id: str
    ) -> SharedAuthorityOutboxEvent:
        row = connection.execute(
            """SELECT o.outbox_id, o.lifecycle_event_id, e.entity_kind, o.entity_id,
            o.lifecycle_version, o.event_state, o.reason_code, o.occurred_at, o.fingerprint
            FROM shared_authority_outbox AS o
            JOIN shared_authority_entities AS e ON e.entity_id = o.entity_id
            WHERE o.lifecycle_event_id = ?""",
            (event_id,),
        ).fetchone()
        if row is None:
            raise SharedAuthorityConflict("SHARED_AUTHORITY_OUTBOX_MISSING")
        return self._outbox_from_row(row)

    @staticmethod
    def _outbox_from_row(row: tuple[object, ...]) -> SharedAuthorityOutboxEvent:
        return SharedAuthorityOutboxEvent(
            outbox_id=str(row[0]),
            lifecycle_event_id=str(row[1]),
            entity_kind=SharedAuthorityEntityKind(str(row[2])),
            entity_id=str(row[3]),
            lifecycle_version=int(row[4]),
            event_state=str(row[5]),
            reason_code=str(row[6]),
            occurred_at=datetime.fromisoformat(str(row[7])),
            fingerprint=str(row[8]),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self._available:
            raise SharedAuthorityUnavailable("shared authority database unavailable")
        connection = self._open_connection()
        try:
            yield connection
        except sqlite3.DatabaseError as error:
            self._fail_closed("SHARED_AUTHORITY_DATABASE_UNAVAILABLE")
            raise SharedAuthorityUnavailable("shared authority database unavailable") from error
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                self._commit(connection)
            except Exception:
                connection.rollback()
                raise

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path, timeout=self._timeout_seconds, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys = ON")
        row = connection.execute("PRAGMA foreign_keys").fetchone()
        if row is None or row[0] != 1:
            connection.close()
            raise SharedAuthorityUnavailable("shared authority foreign-key enforcement unavailable")
        return connection

    @staticmethod
    def _commit(connection: sqlite3.Connection) -> None:
        connection.commit()

    def _fail_closed(self, reason: str) -> None:
        self._available = False
        self._failure_reason = reason


def _migration_checksum(version: int) -> str:
    if version not in (1, 2):
        raise ValueError("unsupported shared authority migration version")
    schema = "|".join(
        _normalize_sql(sql)
        for sql in (*_canonical_table_sql(str(version)), *_canonical_index_sql())
    )
    return sha256(f"shared-authority-migration:{version}:{schema}".encode()).hexdigest()


def _decision_fingerprint(decision: SentinelDecision) -> str:
    """Derive lineage from the evaluated decision; never accept a supplied hash."""
    return _fingerprint(decision.model_dump(mode="json"))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SharedAuthorityConflict("SHARED_AUTHORITY_TIME_INVALID")
    return value.astimezone(UTC)


def _approval_entity_id(approval_id: str) -> str:
    return "shared-sentinel-approval-" + approval_id


def _demo_entity_id(authorization_id: str) -> str:
    return "shared-demo-authorization-" + authorization_id


def _reservation_entity_id(reservation_id: str) -> str:
    return "shared-execution-reservation-" + reservation_id


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall()
    }


def _normalize_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).rstrip(";").lower()


def _table_structure(connection: sqlite3.Connection, table_name: str) -> _TableStructure:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    if row is None or row[0] is None:
        raise SharedAuthorityUnavailable("shared authority required table is missing")
    columns = tuple(
        (item[1], str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    )
    foreign_keys = tuple(
        sorted(
            (
                item[2],
                item[3],
                item[4],
                str(item[5]).upper(),
                str(item[6]).upper(),
                str(item[7]).upper(),
            )
            for item in connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
        )
    )
    unique_indexes: list[tuple[str, ...]] = []
    for item in connection.execute(f"PRAGMA index_list({table_name})").fetchall():
        if int(item[2]) != 1:
            continue
        index_name = str(item[1])
        unique_indexes.append(
            tuple(
                row[2]
                for row in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
            )
        )
    return _TableStructure(
        sql=_normalize_sql(str(row[0])),
        columns=columns,
        foreign_keys=foreign_keys,
        unique_indexes=tuple(sorted(unique_indexes)),
    )


def _index_structure(connection: sqlite3.Connection, index_name: str) -> _IndexStructure:
    row = connection.execute(
        "SELECT sql, tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?", (index_name,)
    ).fetchone()
    if row is None or row[0] is None:
        raise SharedAuthorityUnavailable("shared authority required index is missing")
    table_name = str(row[1])
    index_list = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    matching = next((item for item in index_list if item[1] == index_name), None)
    if matching is None:
        raise SharedAuthorityUnavailable("shared authority index metadata is missing")
    return _IndexStructure(
        sql=_normalize_sql(str(row[0])),
        columns=tuple(
            item[2] for item in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        ),
        unique=bool(matching[2]),
    )


@lru_cache(maxsize=2)
def _canonical_schema_structure(schema_version: str = _SCHEMA_VERSION) -> tuple[
    dict[str, _TableStructure], dict[str, _IndexStructure]
]:
    """Derive the expected introspection signature from the same canonical DDL."""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_canonical_schema(connection, schema_version=schema_version)
        return (
            {name: _table_structure(connection, name) for name in _REQUIRED_TABLES},
            {name: _index_structure(connection, name) for name in _REQUIRED_INDEXES},
        )
    finally:
        connection.close()


def _create_canonical_schema(
    connection: sqlite3.Connection, *, schema_version: str = _SCHEMA_VERSION
) -> None:
    """Create canonical objects used by both bootstrap and strict verification."""
    for statement in _canonical_table_sql(schema_version):
        connection.execute(statement)
    for statement in _canonical_index_sql():
        connection.execute(statement)


def _canonical_table_sql(schema_version: str = _SCHEMA_VERSION) -> tuple[str, ...]:
    if schema_version not in {"1", "2"}:
        raise ValueError("unsupported shared authority schema version")
    cutover_states = (
        "'bootstrap_non_authoritative'"
        if schema_version == "1"
        else "'bootstrap_non_authoritative', 'sentinel_approval_owner'"
    )
    return (
        "CREATE TABLE shared_authority_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE shared_authority_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )""",
        """CREATE TABLE shared_authority_cutover_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            state TEXT NOT NULL CHECK (state IN (""" + cutover_states + """)),
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE shared_authority_entities (
            entity_id TEXT PRIMARY KEY,
            entity_kind TEXT NOT NULL CHECK (entity_kind IN (
                'schema_bootstrap', 'sentinel_approval', 'demo_authorization',
                'execution_reservation'
            )),
            status TEXT NOT NULL,
            lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
            semantic_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE shared_sentinel_approvals (
            entity_id TEXT PRIMARY KEY REFERENCES shared_authority_entities(entity_id)
                ON DELETE RESTRICT,
            approval_id TEXT NOT NULL UNIQUE,
            decision_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            analysis_id TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_fingerprint TEXT NOT NULL,
            assessment_id TEXT NOT NULL,
            assessment_fingerprint TEXT NOT NULL,
            account_id TEXT,
            expires_at TEXT NOT NULL
        )""",
        """CREATE TABLE shared_demo_authorizations (
            entity_id TEXT PRIMARY KEY REFERENCES shared_authority_entities(entity_id)
                ON DELETE RESTRICT,
            authorization_id TEXT NOT NULL UNIQUE,
            approval_entity_id TEXT NOT NULL REFERENCES shared_sentinel_approvals(entity_id)
                ON DELETE RESTRICT,
            execution_intent_id TEXT NOT NULL,
            execution_intent_fingerprint TEXT NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_fingerprint TEXT NOT NULL,
            account_id TEXT NOT NULL,
            account_reference TEXT NOT NULL,
            broker_server TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            broker_symbol TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )""",
        """CREATE TABLE shared_execution_reservations (
            entity_id TEXT PRIMARY KEY REFERENCES shared_authority_entities(entity_id)
                ON DELETE RESTRICT,
            reservation_id TEXT NOT NULL UNIQUE,
            execution_identity TEXT NOT NULL UNIQUE,
            approval_entity_id TEXT NOT NULL REFERENCES shared_sentinel_approvals(entity_id)
                ON DELETE RESTRICT,
            authorization_entity_id TEXT NOT NULL REFERENCES shared_demo_authorizations(entity_id)
                ON DELETE RESTRICT,
            safety_evidence_fingerprint TEXT NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            execution_intent_fingerprint TEXT NOT NULL,
            account_id TEXT NOT NULL,
            account_reference TEXT NOT NULL,
            broker_server TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            broker_symbol TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )""",
        """CREATE TABLE shared_authority_lifecycle_events (
            event_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id)
                ON DELETE RESTRICT,
            lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
            from_status TEXT,
            to_status TEXT NOT NULL,
            event_state TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            UNIQUE(entity_id, lifecycle_version)
        )""",
        """CREATE TABLE shared_authority_outbox (
            outbox_id TEXT PRIMARY KEY,
            lifecycle_event_id TEXT NOT NULL UNIQUE
                REFERENCES shared_authority_lifecycle_events(event_id) ON DELETE RESTRICT,
            entity_id TEXT NOT NULL REFERENCES shared_authority_entities(entity_id)
                ON DELETE RESTRICT,
            lifecycle_version INTEGER NOT NULL CHECK (lifecycle_version >= 1),
            event_state TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            delivered_at TEXT
        )""",
    )


def _canonical_index_sql() -> tuple[str, ...]:
    return (
        """CREATE INDEX shared_authority_lifecycle_entity_version_index
        ON shared_authority_lifecycle_events (entity_id, lifecycle_version)""",
        """CREATE INDEX shared_authority_outbox_pending_index
        ON shared_authority_outbox (delivered_at, lifecycle_version, outbox_id)""",
    )
