"""Read-only integration seams for future independent external authorities.

The repository intentionally has no trusted production implementation.  These
services validate sanitized observations and exact lineage, but their results
are structurally non-authorizing.  A future production integration must add an
independently reviewed provider authentication/trust mechanism and compose it
through a separate production cutover.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.models.demo_execution import ControlledDemoExecutionPolicy
from app.models.independent_governance import (
    BrokerReconciliationAssessment,
    BrokerReconciliationObservation,
    BrokerReconciliationRequest,
    ExternalAssessmentStatus,
    ExternalAuthorityAvailability,
    IndependentPolicyAuthorityAssessment,
    IndependentPolicyAuthorityObservation,
    IndependentPolicyAuthorityRequest,
)
from app.services.sentinel_policy_governance import governance_binding_codes


@runtime_checkable
class ReadOnlyIndependentPolicyAuthorityProvider(Protocol):
    """Future provider contract; protocol conformance does not establish trust."""

    @property
    def provider_id(self) -> str: ...

    def observe(
        self, request: IndependentPolicyAuthorityRequest
    ) -> IndependentPolicyAuthorityObservation: ...


@runtime_checkable
class ReadOnlyBrokerReconciliationProvider(Protocol):
    """Future read-only broker source; it must never expose a submit method."""

    @property
    def provider_id(self) -> str: ...

    def observe(self, request: BrokerReconciliationRequest) -> BrokerReconciliationObservation: ...


class UnavailableIndependentPolicyAuthorityProvider:
    provider_id = "UNCONFIGURED_INDEPENDENT_POLICY_AUTHORITY"

    def observe(
        self, request: IndependentPolicyAuthorityRequest
    ) -> IndependentPolicyAuthorityObservation:
        return IndependentPolicyAuthorityObservation(
            availability=ExternalAuthorityAvailability.UNAVAILABLE,
            provider_id=self.provider_id,
            request_fingerprint=request.request_fingerprint,
            challenge_id=request.challenge_id,
            retrieved_at=request.requested_at,
            reason_codes=("INDEPENDENT_POLICY_AUTHORITY_UNAVAILABLE",),
        )


class UnavailableBrokerReconciliationProvider:
    provider_id = "UNCONFIGURED_BROKER_RECONCILIATION_PROVIDER"

    def observe(self, request: BrokerReconciliationRequest) -> BrokerReconciliationObservation:
        return BrokerReconciliationObservation(
            availability=ExternalAuthorityAvailability.UNAVAILABLE,
            provider_id=self.provider_id,
            request_fingerprint=request.request_fingerprint,
            challenge_id=request.challenge_id,
            retrieved_at=request.requested_at,
            reason_codes=("AUTHORITATIVE_BROKER_RECONCILIATION_UNAVAILABLE",),
        )


class IndependentPolicyAuthorityIntegration:
    """Validate an authority-shaped observation without promoting it to authority."""

    def __init__(self, provider: ReadOnlyIndependentPolicyAuthorityProvider | None = None) -> None:
        self._provider = provider or UnavailableIndependentPolicyAuthorityProvider()

    def assess(
        self,
        *,
        request: IndependentPolicyAuthorityRequest,
        policy: ControlledDemoExecutionPolicy,
        evaluated_at: datetime,
    ) -> IndependentPolicyAuthorityAssessment:
        now = _utc(evaluated_at)
        provider_id = _provider_id(self._provider)
        try:
            observation = self._provider.observe(request)
        except Exception:
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalAssessmentStatus.UNAVAILABLE,
                codes=("INDEPENDENT_POLICY_AUTHORITY_PROVIDER_ERROR",),
            )
        if not isinstance(observation, IndependentPolicyAuthorityObservation):
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalAssessmentStatus.INVALID,
                codes=("INDEPENDENT_POLICY_AUTHORITY_RESULT_INVALID",),
            )

        codes: list[str] = []
        if observation.provider_id != provider_id:
            codes.append("INDEPENDENT_POLICY_AUTHORITY_PROVIDER_MISMATCH")
        if observation.request_fingerprint != request.request_fingerprint:
            codes.append("INDEPENDENT_POLICY_AUTHORITY_REQUEST_MISMATCH")
        if observation.challenge_id != request.challenge_id:
            codes.append("INDEPENDENT_POLICY_AUTHORITY_CHALLENGE_MISMATCH")
        if not _request_matches_policy(request=request, policy=policy):
            codes.append("INDEPENDENT_POLICY_AUTHORITY_POLICY_MISMATCH")
        codes.extend(_observation_time_codes(request=request, observation=observation, now=now))

        if observation.availability is not ExternalAuthorityAvailability.AVAILABLE:
            status = _assessment_status(observation.availability)
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=status,
                codes=tuple(codes) + observation.reason_codes,
                observation=observation,
            )

        if observation.governance is None or observation.approval is None:
            codes.append("INDEPENDENT_POLICY_AUTHORITY_FACTS_MISSING")
        else:
            codes.extend(
                governance_binding_codes(
                    governance=observation.governance,
                    approval=observation.approval,
                    policy=policy,
                    evaluated_at=now,
                )
            )
        if codes:
            status = (
                ExternalAssessmentStatus.STALE
                if any(code.endswith("STALE") or code.endswith("NOT_EFFECTIVE") or code.endswith("EXPIRED") for code in codes)
                else ExternalAssessmentStatus.INVALID
            )
            return _authority_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=status,
                codes=tuple(codes),
                observation=observation,
            )

        governance = observation.governance
        approval = observation.approval
        assert governance is not None and approval is not None
        return IndependentPolicyAuthorityAssessment(
            status=ExternalAssessmentStatus.STRUCTURALLY_VALIDATED,
            request_fingerprint=request.request_fingerprint,
            provider_id=provider_id,
            evaluated_at=now,
            observation_fingerprint=observation.observation_fingerprint,
            source_event_id=observation.source_event_id,
            source_sequence=observation.source_sequence,
            governance_record_id=governance.governance_record_id,
            approval_artifact_id=approval.artifact.approval_artifact_id,
        )


class BrokerReconciliationIntegration:
    """Validate sanitized broker observations without mutating execution state."""

    def __init__(self, provider: ReadOnlyBrokerReconciliationProvider | None = None) -> None:
        self._provider = provider or UnavailableBrokerReconciliationProvider()

    def assess(
        self, *, request: BrokerReconciliationRequest, evaluated_at: datetime
    ) -> BrokerReconciliationAssessment:
        now = _utc(evaluated_at)
        provider_id = _provider_id(self._provider)
        try:
            observation = self._provider.observe(request)
        except Exception:
            return _broker_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalAssessmentStatus.UNAVAILABLE,
                codes=("BROKER_RECONCILIATION_PROVIDER_ERROR",),
            )
        if not isinstance(observation, BrokerReconciliationObservation):
            return _broker_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=ExternalAssessmentStatus.INVALID,
                codes=("BROKER_RECONCILIATION_RESULT_INVALID",),
            )

        codes: list[str] = []
        if observation.provider_id != provider_id:
            codes.append("BROKER_RECONCILIATION_PROVIDER_MISMATCH")
        if observation.request_fingerprint != request.request_fingerprint:
            codes.append("BROKER_RECONCILIATION_REQUEST_MISMATCH")
        if observation.challenge_id != request.challenge_id:
            codes.append("BROKER_RECONCILIATION_CHALLENGE_MISMATCH")
        codes.extend(_observation_time_codes(request=request, observation=observation, now=now))

        if observation.availability in {
            ExternalAuthorityAvailability.UNAVAILABLE,
            ExternalAuthorityAvailability.CONFLICT,
        }:
            return _broker_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=_assessment_status(observation.availability),
                codes=tuple(codes) + observation.reason_codes,
                observation=observation,
            )

        if observation.account_reference != request.account_reference:
            codes.append("BROKER_RECONCILIATION_ACCOUNT_REFERENCE_MISMATCH")
        order = observation.broker_order
        if observation.availability is ExternalAuthorityAvailability.AVAILABLE:
            if order is None:
                codes.append("BROKER_RECONCILIATION_ORDER_MISSING")
            else:
                if order.account_id is None or order.account_id.upper() != request.account_id:
                    codes.append("BROKER_RECONCILIATION_ACCOUNT_MISMATCH")
                if order.broker_order_id != request.broker_order_id:
                    codes.append("BROKER_RECONCILIATION_ORDER_ID_MISMATCH")
                if order.canonical_symbol is None or order.canonical_symbol.upper() != request.canonical_symbol:
                    codes.append("BROKER_RECONCILIATION_SYMBOL_MISMATCH")
                if order.broker_symbol is None or order.broker_symbol.upper() != request.broker_symbol:
                    codes.append("BROKER_RECONCILIATION_BROKER_SYMBOL_MISMATCH")

        if codes:
            status = (
                ExternalAssessmentStatus.STALE
                if any(code.endswith("STALE") for code in codes)
                else ExternalAssessmentStatus.INVALID
            )
            return _broker_failure(
                request=request,
                provider_id=provider_id,
                now=now,
                status=status,
                codes=tuple(codes),
                observation=observation,
            )

        return BrokerReconciliationAssessment(
            status=(
                ExternalAssessmentStatus.STRUCTURALLY_VALIDATED
                if observation.availability is ExternalAuthorityAvailability.AVAILABLE
                else ExternalAssessmentStatus.CONFIRMED_ABSENT
            ),
            request_fingerprint=request.request_fingerprint,
            provider_id=provider_id,
            evaluated_at=now,
            observation_fingerprint=observation.observation_fingerprint,
            source_event_id=observation.source_event_id,
            source_sequence=observation.source_sequence,
            broker_order=order,
        )


def _request_matches_policy(
    *, request: IndependentPolicyAuthorityRequest, policy: ControlledDemoExecutionPolicy
) -> bool:
    return (
        request.policy_id == policy.policy_id
        and request.policy_version == policy.policy_version
        and request.policy_fingerprint == policy.fingerprint
        and request.account_id == policy.allowed_account.account_id.upper()
        and request.execution_mode == policy.mode.value.upper()
        and request.canonical_symbol == policy.instrument.canonical_symbol
        and request.risk_currency == policy.risk_currency
    )


def _observation_time_codes(*, request: object, observation: object, now: datetime) -> tuple[str, ...]:
    requested_at = request.requested_at
    max_age_seconds = request.max_age_seconds
    observed_at = observation.observed_at
    retrieved_at = observation.retrieved_at
    codes: list[str] = []
    if requested_at > now:
        codes.append("EXTERNAL_OBSERVATION_REQUEST_IN_FUTURE")
    if retrieved_at < requested_at or retrieved_at > now:
        codes.append("EXTERNAL_OBSERVATION_RETRIEVAL_TIME_INVALID")
    if observed_at is not None:
        if observed_at > retrieved_at:
            codes.append("EXTERNAL_OBSERVATION_TIME_INVALID")
        elif (now - observed_at).total_seconds() > max_age_seconds:
            codes.append("EXTERNAL_OBSERVATION_STALE")
    return tuple(codes)


def _authority_failure(
    *,
    request: IndependentPolicyAuthorityRequest,
    provider_id: str,
    now: datetime,
    status: ExternalAssessmentStatus,
    codes: tuple[str, ...],
    observation: IndependentPolicyAuthorityObservation | None = None,
) -> IndependentPolicyAuthorityAssessment:
    normalized_codes = tuple(sorted(set(codes))) or ("INDEPENDENT_POLICY_AUTHORITY_UNAVAILABLE",)
    return IndependentPolicyAuthorityAssessment(
        status=status,
        request_fingerprint=request.request_fingerprint,
        provider_id=provider_id,
        evaluated_at=now,
        observation_fingerprint=(observation.observation_fingerprint if observation else None),
        source_event_id=(observation.source_event_id if observation else None),
        source_sequence=(observation.source_sequence if observation else None),
        reason_codes=normalized_codes,
    )


def _broker_failure(
    *,
    request: BrokerReconciliationRequest,
    provider_id: str,
    now: datetime,
    status: ExternalAssessmentStatus,
    codes: tuple[str, ...],
    observation: BrokerReconciliationObservation | None = None,
) -> BrokerReconciliationAssessment:
    normalized_codes = tuple(sorted(set(codes))) or ("BROKER_RECONCILIATION_UNAVAILABLE",)
    return BrokerReconciliationAssessment(
        status=status,
        request_fingerprint=request.request_fingerprint,
        provider_id=provider_id,
        evaluated_at=now,
        observation_fingerprint=(observation.observation_fingerprint if observation else None),
        source_event_id=(observation.source_event_id if observation else None),
        source_sequence=(observation.source_sequence if observation else None),
        reason_codes=normalized_codes,
    )


def _assessment_status(availability: ExternalAuthorityAvailability) -> ExternalAssessmentStatus:
    return {
        ExternalAuthorityAvailability.NOT_FOUND: ExternalAssessmentStatus.INVALID,
        ExternalAuthorityAvailability.UNAVAILABLE: ExternalAssessmentStatus.UNAVAILABLE,
        ExternalAuthorityAvailability.CONFLICT: ExternalAssessmentStatus.CONFLICT,
        ExternalAuthorityAvailability.AVAILABLE: ExternalAssessmentStatus.INVALID,
    }[availability]


def _provider_id(provider: object) -> str:
    try:
        value = getattr(provider, "provider_id", "")
    except Exception:
        return "INVALID_EXTERNAL_PROVIDER"
    normalized = str(value).strip().upper()
    return normalized or "INVALID_EXTERNAL_PROVIDER"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external trust evaluation time requires timezone awareness")
    return value.astimezone(UTC)
