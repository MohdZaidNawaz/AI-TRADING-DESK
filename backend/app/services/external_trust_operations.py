"""Phase 19 composition of validated external-trust facts.

This service performs no I/O.  It composes Phase 16/18 assessments that have
already passed signature, binding, freshness, and durable replay validation.
Even a complete synthetic composition remains non-authoritative because no
independently administered provider is provisioned in this repository.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.authenticated_external_trust import (
    AuthoritativeReconciliationLineage,
    ExternalTrustAttestationAssessment,
    ExternalTrustCapability,
    ExternalTrustIntegrationStatus,
    ExternalTrustLifecycle,
    RollbackWitnessLineage,
)
from app.models.execution_state import ReconciliationReport
from app.models.external_trust_operations import (
    ExternalOperationsStatus,
    ExternalTransportContext,
    ExternalTrustOperationalAssessment,
)
from app.models.trusted_execution_evidence import ValidatedExecutionEvidence
from app.services.authenticated_external_trust import build_broker_evidence_lineage


class ExternalTrustOperationalCoordinator:
    """Cross-check five separate capabilities without granting authority."""

    def assess(
        self,
        *,
        evaluated_at: datetime,
        transport: ExternalTransportContext | None,
        root_provisioning: ExternalTrustAttestationAssessment | None,
        lifecycle: ExternalTrustAttestationAssessment | None,
        witness: ExternalTrustAttestationAssessment | None,
        broker: ExternalTrustAttestationAssessment | None,
        reconciliation: ExternalTrustAttestationAssessment | None,
        evidence: ValidatedExecutionEvidence | None,
        broker_reconciliation: ReconciliationReport | None,
        expected_witness: RollbackWitnessLineage | None,
        expected_reconciliation: AuthoritativeReconciliationLineage | None,
    ) -> ExternalTrustOperationalAssessment:
        now = _utc(evaluated_at)
        provider_id = (
            "UNCONFIGURED_EXTERNAL_TRUST_PROVIDER"
            if transport is None
            else transport.provider_id
        )
        if transport is None or not transport.configured:
            return _failure(
                now,
                provider_id,
                ExternalOperationsStatus.UNCONFIGURED,
                "EXTERNAL_OPERATIONAL_TRANSPORT_UNCONFIGURED",
            )

        components = (
            (root_provisioning, ExternalTrustCapability.ROOT_PROVISIONING),
            (lifecycle, ExternalTrustCapability.LIFECYCLE_STATUS),
            (witness, ExternalTrustCapability.ROLLBACK_WITNESS),
            (broker, ExternalTrustCapability.BROKER_EVIDENCE_AUTHENTICATION),
            (reconciliation, ExternalTrustCapability.AUTHORITATIVE_RECONCILIATION),
        )
        codes: list[str] = []
        for assessment, capability in components:
            if assessment is None:
                codes.append(f"{capability.value.upper()}_ASSESSMENT_UNAVAILABLE")
                continue
            if assessment.capability is not capability:
                codes.append(f"{capability.value.upper()}_CAPABILITY_MISMATCH")
            if assessment.provider_id != provider_id:
                codes.append("EXTERNAL_OPERATIONAL_PROVIDER_MISMATCH")
            if assessment.status is not ExternalTrustIntegrationStatus.CONFIGURED_UNAUTHENTICATED:
                codes.extend(assessment.reason_codes)
            if (
                not assessment.signature_verified
                or not assessment.bindings_verified
                or not assessment.replay_consumed
                or assessment.validated_statement is None
            ):
                codes.append(f"{capability.value.upper()}_VALIDATION_INCOMPLETE")

        if codes:
            return _failure(
                now,
                provider_id,
                _status_for_components(components),
                *codes,
            )

        assert all(item is not None for item, _ in components)
        assert root_provisioning is not None
        assert lifecycle is not None
        assert witness is not None
        assert broker is not None
        assert reconciliation is not None
        statements = tuple(
            item.validated_statement
            for item in (root_provisioning, lifecycle, witness, broker, reconciliation)
        )
        assert all(statement is not None for statement in statements)
        root_statement, lifecycle_statement, witness_statement, broker_statement, reconciliation_statement = statements
        assert root_statement is not None
        assert lifecycle_statement is not None
        assert witness_statement is not None
        assert broker_statement is not None
        assert reconciliation_statement is not None

        root_identity = (
            root_statement.verification_authority_id,
            root_statement.verification_root_id,
            root_statement.verification_root_version,
            root_statement.verification_root_fingerprint,
        )
        if any(
            (
                statement.verification_authority_id,
                statement.verification_root_id,
                statement.verification_root_version,
                statement.verification_root_fingerprint,
            )
            != root_identity
            for statement in statements[1:]
            if statement is not None
        ):
            codes.append("EXTERNAL_OPERATIONAL_VERIFICATION_ROOT_CONFLICT")
        if (
            root_statement.subject_fingerprint
            != root_statement.verification_root_fingerprint
        ):
            codes.append("EXTERNAL_ROOT_PROVISIONING_SUBJECT_MISMATCH")
        if root_statement.lifecycle is not ExternalTrustLifecycle.ACTIVE:
            codes.append("EXTERNAL_ROOT_PROVISIONING_NOT_ACTIVE")
        if (
            lifecycle_statement.subject_fingerprint != root_statement.subject_fingerprint
            or lifecycle_statement.subject_generation != root_statement.subject_generation
            or lifecycle_statement.scope_fingerprint != root_statement.scope_fingerprint
        ):
            codes.append("EXTERNAL_LIFECYCLE_ROOT_LINEAGE_MISMATCH")
        if lifecycle_statement.lifecycle is not ExternalTrustLifecycle.ACTIVE:
            codes.append("EXTERNAL_LIFECYCLE_NOT_ACTIVE")

        if expected_witness is None or witness_statement.witness_lineage != expected_witness:
            codes.append("EXTERNAL_WITNESS_CHECKPOINT_MISMATCH")
        elif expected_witness.update_outcome != "confirmed":
            codes.append("EXTERNAL_WITNESS_UPDATE_UNCERTAIN")

        if evidence is None or broker_reconciliation is None:
            codes.append("EXTERNAL_BROKER_EVIDENCE_CONTEXT_UNAVAILABLE")
        else:
            try:
                expected_broker = build_broker_evidence_lineage(
                    evidence, broker_reconciliation
                )
            except ValueError:
                codes.append("EXTERNAL_BROKER_EVIDENCE_CONTEXT_INVALID")
            else:
                if broker_statement.broker_lineage != expected_broker:
                    codes.append("EXTERNAL_BROKER_EVIDENCE_LINEAGE_MISMATCH")

        if (
            expected_reconciliation is None
            or reconciliation_statement.reconciliation_lineage
            != expected_reconciliation
        ):
            codes.append("EXTERNAL_RECONCILIATION_LINEAGE_MISMATCH")
        elif evidence is None or (
            expected_reconciliation.evidence_fingerprint != evidence.fingerprint
            or expected_reconciliation.evidence_request_fingerprint
            != evidence.request.fingerprint
        ):
            codes.append("EXTERNAL_RECONCILIATION_EVIDENCE_MISMATCH")
        elif (
            expected_reconciliation.status != "matched"
            or not expected_reconciliation.coverage_complete
        ):
            codes.append("EXTERNAL_RECONCILIATION_INCOMPLETE")

        if codes:
            return _failure(
                now,
                provider_id,
                ExternalOperationsStatus.CONFLICT,
                *codes,
            )

        return ExternalTrustOperationalAssessment(
            status=ExternalOperationsStatus.STRUCTURALLY_VALIDATED_UNAUTHENTICATED,
            provider_id=provider_id,
            evaluated_at=now,
            transport_fingerprint=transport.fingerprint,
            root_assessment_fingerprint=root_provisioning.fingerprint,
            lifecycle_assessment_fingerprint=lifecycle.fingerprint,
            witness_assessment_fingerprint=witness.fingerprint,
            broker_assessment_fingerprint=broker.fingerprint,
            reconciliation_assessment_fingerprint=reconciliation.fingerprint,
            local_bindings_verified=True,
            replay_consumed=True,
            reason_codes=(
                "EXTERNAL_OPERATIONAL_AUTHORITIES_NOT_INDEPENDENTLY_PROVISIONED",
            ),
        )


def _status_for_components(
    components: tuple[
        tuple[ExternalTrustAttestationAssessment | None, ExternalTrustCapability], ...
    ],
) -> ExternalOperationsStatus:
    statuses = {item.status for item, _ in components if item is not None}
    if ExternalTrustIntegrationStatus.STALE in statuses:
        return ExternalOperationsStatus.STALE
    if ExternalTrustIntegrationStatus.CONFLICT in statuses:
        return ExternalOperationsStatus.CONFLICT
    if statuses.intersection(
        {
            ExternalTrustIntegrationStatus.UNAVAILABLE,
            ExternalTrustIntegrationStatus.UNCONFIGURED,
        }
    ):
        return ExternalOperationsStatus.UNAVAILABLE
    return ExternalOperationsStatus.INVALID


def _failure(
    now: datetime,
    provider_id: str,
    status: ExternalOperationsStatus,
    *codes: str,
) -> ExternalTrustOperationalAssessment:
    return ExternalTrustOperationalAssessment(
        status=status,
        provider_id=provider_id,
        evaluated_at=now,
        reason_codes=tuple(sorted(set(codes)))
        or ("EXTERNAL_OPERATIONAL_INTEGRATION_UNAVAILABLE",),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external operations evaluation requires timezone awareness")
    return value.astimezone(UTC)
