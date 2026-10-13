"""Phase 13B adapter over durable lifecycle state, with no execution authority.

The adapter deliberately labels every result ``DURABLE_LIFECYCLE_ONLY``.  It
does not consult the current process-local Sentinel or demo-authorization
owners, so it must not be installed as an authoritative execution dependency.
"""

from __future__ import annotations

from app.execution.durable_authority_store import (
    DurableAuthorityState,
    DurableAuthorityStore,
    DurableAuthorityStoreError,
)
from app.models.authority_protocol import (
    ApprovalAuthorityRequest,
    AuthorityBackendScope,
    AuthorityOperation,
    AuthorityOperationOutcome,
    AuthorityOperationResult,
    AuthorityRecordKind,
    AuthorityTransitionRequest,
    AuthorityLifecycleProtocol,
    DemoAuthorizationAuthorityRequest,
)
from app.models.durable_authority import (
    DurableApprovalState,
    DurableDemoAuthorizationState,
)


class DurableAuthorityProtocolAdapter:
    """Typed adapter for ``DurableAuthorityStore`` lifecycle records only."""

    def __init__(self, store: DurableAuthorityStore) -> None:
        self._store = store

    @property
    def backend_scope(self) -> AuthorityBackendScope:
        return AuthorityBackendScope.DURABLE_LIFECYCLE_ONLY

    def register_approval(self, request: ApprovalAuthorityRequest) -> AuthorityOperationResult:
        return self._approval(request, action="register")

    def verify_approval(self, request: ApprovalAuthorityRequest) -> AuthorityOperationResult:
        return self._approval(request, action="verify")

    def consume_approval(self, request: ApprovalAuthorityRequest) -> AuthorityOperationResult:
        return self._approval(request, action="consume")

    def register_demo_authorization(
        self, request: DemoAuthorizationAuthorityRequest
    ) -> AuthorityOperationResult:
        return self._authorization(request, action="register")

    def verify_demo_authorization(
        self, request: DemoAuthorizationAuthorityRequest
    ) -> AuthorityOperationResult:
        return self._authorization(request, action="verify")

    def consume_demo_authorization(
        self, request: DemoAuthorizationAuthorityRequest
    ) -> AuthorityOperationResult:
        return self._authorization(request, action="consume")

    def transition(self, request: AuthorityTransitionRequest) -> AuthorityOperationResult:
        try:
            previous = self._state(request.record_kind, request.external_id)
            if request.operation is AuthorityOperation.REVOKE:
                state = (
                    self._store.revoke_approval(request.external_id, now=request.requested_at)
                    if request.record_kind is AuthorityRecordKind.APPROVAL
                    else self._store.revoke_demo_authorization(request.external_id, now=request.requested_at)
                )
            else:
                state = self._store.mark_reconciliation_required(
                    kind=request.record_kind.value,
                    external_id=request.external_id,
                    now=request.requested_at,
                )
            return _success(
                request.record_kind,
                request.operation,
                state,
                created=previous is None or previous.status is not state.status,
            )
        except DurableAuthorityStoreError as error:
            return _rejected(request.record_kind, request.operation, request.external_id, str(error), self._state(request.record_kind, request.external_id))

    def _approval(self, request: ApprovalAuthorityRequest, *, action: str) -> AuthorityOperationResult:
        if _operation_for(action) is not request.operation:
            return _rejected(AuthorityRecordKind.APPROVAL, request.operation, request.decision.approval_id or "missing-approval-id", "AUTHORITY_OPERATION_MISMATCH")
        external_id = request.decision.approval_id or "missing-approval-id"
        try:
            if action == "register":
                state, created = self._store.register_approval(request.decision, now=request.requested_at)
                return _success(AuthorityRecordKind.APPROVAL, request.operation, state, created=created)
            if action == "verify":
                state = self._store.verify_approval(request.decision, now=request.requested_at)
                return _success(AuthorityRecordKind.APPROVAL, request.operation, state)
            state, consumed = self._store.consume_approval(request.decision, now=request.requested_at)
            return _success(AuthorityRecordKind.APPROVAL, request.operation, state, created=consumed)
        except DurableAuthorityStoreError as error:
            return _rejected(AuthorityRecordKind.APPROVAL, request.operation, external_id, str(error), self._state(AuthorityRecordKind.APPROVAL, external_id))

    def _authorization(
        self, request: DemoAuthorizationAuthorityRequest, *, action: str
    ) -> AuthorityOperationResult:
        if _operation_for(action) is not request.operation:
            return _rejected(AuthorityRecordKind.DEMO_AUTHORIZATION, request.operation, request.authorization.authorization_id, "AUTHORITY_OPERATION_MISMATCH")
        external_id = request.authorization.authorization_id
        try:
            if action == "register":
                state, created = self._store.issue_demo_authorization(request.authorization, now=request.requested_at)
                return _success(AuthorityRecordKind.DEMO_AUTHORIZATION, request.operation, state, created=created)
            if action == "verify":
                state = self._store.verify_demo_authorization(request.authorization, now=request.requested_at)
                return _success(AuthorityRecordKind.DEMO_AUTHORIZATION, request.operation, state)
            state, consumed = self._store.consume_demo_authorization(request.authorization, now=request.requested_at)
            return _success(AuthorityRecordKind.DEMO_AUTHORIZATION, request.operation, state, created=consumed)
        except DurableAuthorityStoreError as error:
            return _rejected(AuthorityRecordKind.DEMO_AUTHORIZATION, request.operation, external_id, str(error), self._state(AuthorityRecordKind.DEMO_AUTHORIZATION, external_id))

    def _state(
        self, record_kind: AuthorityRecordKind, external_id: str
    ) -> DurableAuthorityState | None:
        try:
            return (
                self._store.get_approval(external_id)
                if record_kind is AuthorityRecordKind.APPROVAL
                else self._store.get_demo_authorization(external_id)
            )
        except DurableAuthorityStoreError:
            return None


def _operation_for(action: str) -> AuthorityOperation:
    return {
        "register": AuthorityOperation.REGISTER,
        "verify": AuthorityOperation.VERIFY,
        "consume": AuthorityOperation.CONSUME,
    }[action]


def _success(
    kind: AuthorityRecordKind,
    operation: AuthorityOperation,
    state: DurableAuthorityState,
    *,
    created: bool = True,
) -> AuthorityOperationResult:
    return AuthorityOperationResult(
        record_kind=kind,
        operation=operation,
        outcome=(AuthorityOperationOutcome.APPLIED if created else AuthorityOperationOutcome.ALREADY_APPLIED),
        backend_scope=AuthorityBackendScope.DURABLE_LIFECYCLE_ONLY,
        external_id=(state.approval_id if isinstance(state, DurableApprovalState) else state.authorization_id),
        status=state.status.value,
        lifecycle_version=state.lifecycle_version,
        source_fingerprint=state.source_fingerprint,
        expires_at=(state.decision.expires_at if isinstance(state, DurableApprovalState) else state.authorization.expires_at),
    )


def _rejected(
    kind: AuthorityRecordKind,
    operation: AuthorityOperation,
    external_id: str,
    reason_code: str,
    state: DurableAuthorityState | None = None,
) -> AuthorityOperationResult:
    return AuthorityOperationResult(
        record_kind=kind,
        operation=operation,
        outcome=AuthorityOperationOutcome.REJECTED,
        backend_scope=AuthorityBackendScope.DURABLE_LIFECYCLE_ONLY,
        external_id=external_id,
        status=None if state is None else state.status.value,
        lifecycle_version=None if state is None else state.lifecycle_version,
        source_fingerprint=None if state is None else state.source_fingerprint,
        expires_at=None if state is None else (state.decision.expires_at if isinstance(state, DurableApprovalState) else state.authorization.expires_at),
        reason_code=reason_code,
    )
