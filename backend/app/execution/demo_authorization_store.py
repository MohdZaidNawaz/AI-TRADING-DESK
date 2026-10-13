"""Bounded, thread-safe process-local store for one demo pre-authorization.

The store never evicts an authorization.  A new process begins in recovery
uncertainty and must be explicitly marked as clean by a future recovery
workflow before it can create an artifact.  That makes a restart fail closed
instead of silently recreating demo authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING

from app.models.demo_authorization import ControlledDemoAuthorization, DemoAuthorizationStatus

if TYPE_CHECKING:
    from app.models.authority_protocol import AuthorityLifecycleProtocol


class DemoAuthorizationStoreError(ValueError):
    """A deterministic fail-closed authorization registry error."""


class DemoAuthorizationStore:
    """Strictly bounded registry for the first and only demo identity."""

    def __init__(
        self,
        *,
        max_records: int = 1,
        authority_protocol: "AuthorityLifecycleProtocol | None" = None,
        require_authoritative_protocol: bool = False,
    ) -> None:
        if max_records != 1:
            raise ValueError("first controlled demo authorization store permits exactly one record")
        if require_authoritative_protocol:
            # Phase 13B exposes no trusted durable authorization source yet.
            # A protocol candidate or test fake cannot be promoted by a flag.
            raise DemoAuthorizationStoreError(
                "AUTHORITATIVE_DURABLE_DEMO_AUTHORIZATION_BACKEND_UNAVAILABLE"
            )
        self._records: dict[str, ControlledDemoAuthorization] = {}
        self._recovery_uncertain = True
        self._lock = RLock()
        # Candidate-only DI seam.  Existing methods intentionally continue to
        # use this store exclusively until the shared-authority migration.
        self._candidate_authority_protocol = authority_protocol

    @property
    def authority_integration_state(self) -> str:
        return (
            "LEGACY_PROCESS_LOCAL"
            if self._candidate_authority_protocol is None
            else "CANDIDATE_PROTOCOL_DISCONNECTED"
        )

    def confirm_clean_start(self) -> None:
        """Explicitly acknowledge a fresh, reconciled state before authorization.

        The Phase 11 service never calls this method.  A future controlled
        rollout must use an independently reviewed recovery procedure.
        """
        with self._lock:
            if self._records:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_ALREADY_EXISTS")
            self._recovery_uncertain = False

    def mark_recovery_uncertain(self, *, now: datetime) -> None:
        self._require_aware(now)
        with self._lock:
            self._recovery_uncertain = True
            for identifier, authorization in tuple(self._records.items()):
                if authorization.status is DemoAuthorizationStatus.AUTHORIZED:
                    self._records[identifier] = authorization.model_copy(
                        update={"status": DemoAuthorizationStatus.RECONCILIATION_REQUIRED}
                    )

    def create(self, authorization: ControlledDemoAuthorization) -> ControlledDemoAuthorization:
        with self._lock:
            if self._recovery_uncertain:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_RECOVERY_REQUIRED")
            if authorization.authorization_id in self._records:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_DUPLICATE")
            if self._records:
                raise DemoAuthorizationStoreError("DEMO_SINGLE_AUTHORIZATION_LIMIT")
            self._records[authorization.authorization_id] = authorization
            return authorization

    def get(self, authorization_id: str) -> ControlledDemoAuthorization | None:
        with self._lock:
            return self._records.get(authorization_id)

    def verify(self, *, authorization_id: str, now: datetime) -> ControlledDemoAuthorization:
        self._require_aware(now)
        now_utc = now.astimezone(UTC)
        with self._lock:
            if self._recovery_uncertain:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_RECOVERY_REQUIRED")
            authorization = self._records.get(authorization_id)
            if authorization is None:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_NOT_FOUND")
            if authorization.status is DemoAuthorizationStatus.AUTHORIZED and now_utc >= authorization.expires_at:
                authorization = authorization.model_copy(update={"status": DemoAuthorizationStatus.EXPIRED})
                self._records[authorization_id] = authorization
            if authorization.status is not DemoAuthorizationStatus.AUTHORIZED:
                raise DemoAuthorizationStoreError(f"DEMO_AUTHORIZATION_{authorization.status.value.upper()}")
            return authorization

    def revoke(self, *, authorization_id: str, reason_code: str, now: datetime) -> ControlledDemoAuthorization:
        self._require_aware(now)
        if not reason_code.strip():
            raise ValueError("authorization revocation requires a reason code")
        with self._lock:
            authorization = self._records.get(authorization_id)
            if authorization is None:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_NOT_FOUND")
            if authorization.status is DemoAuthorizationStatus.CONSUMED:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_CONSUMED")
            updated = authorization.model_copy(update={"status": DemoAuthorizationStatus.REVOKED})
            self._records[authorization_id] = updated
            return updated

    def mark_reconciliation_required(self, *, authorization_id: str, now: datetime) -> ControlledDemoAuthorization:
        self._require_aware(now)
        with self._lock:
            authorization = self._records.get(authorization_id)
            if authorization is None:
                raise DemoAuthorizationStoreError("DEMO_AUTHORIZATION_NOT_FOUND")
            updated = authorization.model_copy(
                update={"status": DemoAuthorizationStatus.RECONCILIATION_REQUIRED}
            )
            self._records[authorization_id] = updated
            return updated

    def consume_for_future_submission(self, *, authorization_id: str, now: datetime) -> ControlledDemoAuthorization:
        """Reserved for a future real submission boundary; never called in Phase 11."""
        authorization = self.verify(authorization_id=authorization_id, now=now)
        with self._lock:
            consumed = authorization.model_copy(update={"status": DemoAuthorizationStatus.CONSUMED})
            self._records[authorization_id] = consumed
            return consumed

    def snapshot(self) -> tuple[ControlledDemoAuthorization, ...]:
        with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorization store timestamps require timezone awareness")
