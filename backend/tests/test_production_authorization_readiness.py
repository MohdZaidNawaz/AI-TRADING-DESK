"""Phase 13G governance-readiness tests use synthetic, non-submitting inputs."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.audit.service import AuditService
from app.audit.store import AuditStore
from app.execution.kill_switch import ExecutionKillSwitch
from app.models.production_authorization import (
    ProductionAuthorizationReadinessReport,
    ProductionReadinessCheck,
    ProductionReadinessCheckStatus,
    ProductionReadinessPrerequisite,
)
from app.models.sentinel_policy_authority import (
    SentinelPolicyAuthorityLevel,
    SentinelPolicyLifecycle,
    SentinelPolicyResolution,
    SentinelPolicyResolutionStatus,
)
from app.services.production_authorization_readiness import (
    ProductionAuthorizationReadinessGate,
)
from app.services.durable_independent_replay import DurableIndependentReplayStore
from app.services.sentinel_policy_authority import SentinelPortfolioRiskPolicyVerifier
from app.services.sentinel_risk_evidence import SentinelPortfolioRiskEvidenceVerifier
from app.services.trusted_execution_evidence import TrustedExecutionEvidenceService
from tests.test_demo_execution_readiness import NOW, _pair, _policy
from tests.test_durable_reservation_store import _reservation
from tests.test_sentinel_policy_authority import (
    _bounded_policy,
    _test_policy_record,
    _test_policy_verifier,
)
from tests.test_sentinel_risk_evidence import _inputs
from tests.test_trusted_execution_evidence import (
    NOW as EVIDENCE_NOW,
    _FakeProvider,
    _request,
    _response,
)


def _by_prerequisite(report: ProductionAuthorizationReadinessReport):
    return {check.prerequisite: check for check in report.checks}


def test_default_report_is_complete_deterministic_and_non_authorizing() -> None:
    first = ProductionAuthorizationReadinessGate().evaluate(now=NOW)
    second = ProductionAuthorizationReadinessGate().evaluate(now=NOW)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert len(first.checks) == len(ProductionReadinessPrerequisite)
    assert tuple(check.prerequisite for check in first.checks) == tuple(
        sorted(ProductionReadinessPrerequisite, key=lambda item: item.value)
    )
    assert first.status == "blocked"
    assert first.production_authorization_available is False
    assert first.submission_allowed is False
    assert first.execution_authority is False
    assert all(check.authorizes_execution is False for check in first.checks)


@pytest.mark.parametrize(
    "field",
    ("production_authorization_available", "submission_allowed", "execution_authority"),
)
def test_report_cannot_be_constructed_with_authority(field: str) -> None:
    report = ProductionAuthorizationReadinessGate().evaluate(now=NOW)
    payload = report.model_dump(exclude={"fingerprint"})
    payload[field] = True
    with pytest.raises(ValidationError):
        ProductionAuthorizationReadinessReport.model_validate(payload)


def test_individual_check_cannot_be_constructed_with_execution_authority() -> None:
    report = ProductionAuthorizationReadinessGate().evaluate(now=NOW)
    payload = report.checks[0].model_dump()
    payload["authorizes_execution"] = True
    with pytest.raises(ValidationError):
        type(report.checks[0]).model_validate(payload)


def test_report_and_nested_checks_are_immutable() -> None:
    report = ProductionAuthorizationReadinessGate().evaluate(now=NOW)
    with pytest.raises(ValidationError):
        report.status = "ready"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        report.checks[0].status = ProductionReadinessCheckStatus.SATISFIED  # type: ignore[misc]


@pytest.mark.parametrize("reason_codes", (("",), ("lowercase",), ("BAD-CODE",)))
def test_reason_codes_must_be_nonempty_canonical_identifiers(
    reason_codes: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match="canonical"):
        ProductionReadinessCheck(
            prerequisite=ProductionReadinessPrerequisite.EXECUTION_MODE,
            status=ProductionReadinessCheckStatus.BLOCKED,
            reason_codes=reason_codes,
            checked_at=NOW,
        )


def test_satisfied_check_requires_safe_source_provenance() -> None:
    with pytest.raises(ValidationError, match="source provenance"):
        ProductionReadinessCheck(
            prerequisite=ProductionReadinessPrerequisite.AUDIT_DURABILITY,
            status=ProductionReadinessCheckStatus.SATISFIED,
            checked_at=NOW,
        )
    with pytest.raises(ValidationError, match="non-empty printable"):
        ProductionReadinessCheck(
            prerequisite=ProductionReadinessPrerequisite.AUDIT_DURABILITY,
            status=ProductionReadinessCheckStatus.SATISFIED,
            source_reference="   ",
            checked_at=NOW,
        )
    with pytest.raises(ValidationError, match="cannot contain secrets"):
        ProductionReadinessCheck(
            prerequisite=ProductionReadinessPrerequisite.AUDIT_DURABILITY,
            status=ProductionReadinessCheckStatus.SATISFIED,
            source_reference="api_key: should-not-appear",
            checked_at=NOW,
        )


def test_missing_or_duplicate_prerequisites_are_rejected() -> None:
    report = ProductionAuthorizationReadinessGate().evaluate(now=NOW)
    payload = report.model_dump(exclude={"fingerprint"})
    payload["checks"] = payload["checks"][:-1]
    with pytest.raises(ValidationError, match="every prerequisite once"):
        ProductionAuthorizationReadinessReport.model_validate(payload)

    payload = report.model_dump(exclude={"fingerprint"})
    payload["checks"] = (*payload["checks"][:-1], payload["checks"][0])
    with pytest.raises(ValidationError, match="every prerequisite once"):
        ProductionAuthorizationReadinessReport.model_validate(payload)


def test_report_rejects_checks_from_different_evaluation_times() -> None:
    report = ProductionAuthorizationReadinessGate().evaluate(now=NOW)
    payload = report.model_dump(exclude={"fingerprint"})
    checks = list(payload["checks"])
    checks[0] = {**checks[0], "checked_at": NOW + timedelta(seconds=1)}
    payload["checks"] = tuple(checks)
    with pytest.raises(ValidationError, match="one evaluation timestamp"):
        ProductionAuthorizationReadinessReport.model_validate(payload)


def test_test_only_policy_can_prove_integrity_but_never_governance_authority() -> None:
    policy = _bounded_policy()
    resolution = _test_policy_verifier((_test_policy_record(policy),)).resolve(
        candidate=policy,
        evaluated_at=NOW,
    )
    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW,
        policy=policy,
        policy_resolution=resolution,
    )
    checks = _by_prerequisite(report)

    assert checks[ProductionReadinessPrerequisite.POLICY_INTEGRITY].status is ProductionReadinessCheckStatus.SATISFIED
    for prerequisite in (
        ProductionReadinessPrerequisite.AUTHORIZED_POLICY_APPROVER,
        ProductionReadinessPrerequisite.INDEPENDENT_TRUST_ANCHOR,
        ProductionReadinessPrerequisite.AUTHORITATIVE_APPROVAL_SOURCE,
        ProductionReadinessPrerequisite.AUTHORITATIVE_REVOCATION_SOURCE,
    ):
        assert checks[prerequisite].status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert report.production_authorization_available is False


@pytest.mark.parametrize(
    ("lifecycle", "changes", "expected"),
    [
        (
            SentinelPolicyLifecycle.REVOKED,
            {"lifecycle_version": 2, "revoked_reason": "test revocation"},
            ProductionReadinessCheckStatus.REVOKED,
        ),
        (
            SentinelPolicyLifecycle.SUPERSEDED,
            {"lifecycle_version": 2, "superseded_by_version": "v2"},
            ProductionReadinessCheckStatus.SUPERSEDED,
        ),
    ],
)
def test_revoked_and_superseded_policy_resolutions_remain_distinct(
    lifecycle: SentinelPolicyLifecycle,
    changes: dict[str, object],
    expected: ProductionReadinessCheckStatus,
) -> None:
    policy = _bounded_policy()
    record = _test_policy_record(policy, lifecycle=lifecycle, **changes)
    resolution = _test_policy_verifier((record,)).resolve(
        candidate=policy,
        evaluated_at=NOW,
    )
    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW,
        policy=policy,
        policy_resolution=resolution,
    )
    assert _by_prerequisite(report)[ProductionReadinessPrerequisite.POLICY_INTEGRITY].status is expected


def test_policy_failure_classification_uses_exact_codes_not_substrings() -> None:
    policy = _bounded_policy()
    resolution = SentinelPolicyResolution(
        status=SentinelPolicyResolutionStatus.REJECTED,
        authority_level=SentinelPolicyAuthorityLevel.UNAVAILABLE,
        requested_policy_id=policy.policy_id,
        requested_policy_version=policy.policy_version,
        requested_policy_fingerprint=policy.fingerprint,
        source_id="synthetic-rejected-source",
        resolved_at=NOW,
        reason_codes=("NOT_REVOKED_DATA",),
    )
    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW,
        policy=policy,
        policy_resolution=resolution,
    )
    check = _by_prerequisite(report)[ProductionReadinessPrerequisite.POLICY_INTEGRITY]
    assert check.status is ProductionReadinessCheckStatus.INVALID


def test_expired_or_mismatched_policy_fails_closed() -> None:
    expired = _policy(
        effective_at=NOW - timedelta(minutes=10),
        expires_at=NOW - timedelta(minutes=1),
    )
    expired_resolution = _test_policy_verifier((_test_policy_record(expired),)).resolve(
        candidate=expired,
        evaluated_at=NOW,
    )
    expired_check = _by_prerequisite(
        ProductionAuthorizationReadinessGate().evaluate(
            now=NOW,
            policy=expired,
            policy_resolution=expired_resolution,
        )
    )[ProductionReadinessPrerequisite.POLICY_INTEGRITY]
    assert expired_check.status is ProductionReadinessCheckStatus.STALE

    policy = _bounded_policy()
    resolution = _test_policy_verifier((_test_policy_record(policy),)).resolve(
        candidate=policy,
        evaluated_at=NOW,
    )
    changed = policy.model_copy(update={"max_order_volume": policy.max_order_volume + 1})
    mismatch = _by_prerequisite(
        ProductionAuthorizationReadinessGate().evaluate(
            now=NOW,
            policy=changed,
            policy_resolution=resolution,
        )
    )[ProductionReadinessPrerequisite.POLICY_INTEGRITY]
    assert mismatch.status is ProductionReadinessCheckStatus.INVALID


def test_structural_execution_evidence_is_freshness_checked_but_not_trusted() -> None:
    evidence = TrustedExecutionEvidenceService(_FakeProvider(_response())).assess(
        _request(),
        now=EVIDENCE_NOW,
    )
    assert evidence.ready is True
    fresh = ProductionAuthorizationReadinessGate().evaluate(
        now=EVIDENCE_NOW,
        trusted_evidence=evidence,
    )
    stale = ProductionAuthorizationReadinessGate().evaluate(
        now=EVIDENCE_NOW + timedelta(seconds=evidence.request.max_age_seconds + 1),
        trusted_evidence=evidence,
    )
    fresh_check = _by_prerequisite(fresh)[ProductionReadinessPrerequisite.TRUSTED_EXECUTION_EVIDENCE]
    assert fresh_check.status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert fresh_check.reason_codes == (
        "TRUSTED_EXECUTION_EVIDENCE_PROVIDER_UNAUTHENTICATED",
    )
    assert _by_prerequisite(stale)[ProductionReadinessPrerequisite.TRUSTED_EXECUTION_EVIDENCE].status is ProductionReadinessCheckStatus.STALE
    assert fresh.submission_allowed is stale.submission_allowed is False


def test_test_only_risk_evidence_and_reservation_do_not_become_authority() -> None:
    policy, proposal, decision, intent, evidence = _inputs()
    risk = SentinelPortfolioRiskEvidenceVerifier(
        _test_policy_verifier((_test_policy_record(policy),))
    ).verify(
        policy=policy,
        proposal=proposal,
        decision=decision,
        intent=intent,
        evidence=evidence,
        now=NOW,
    )
    report = ProductionAuthorizationReadinessGate().evaluate(
        now=NOW,
        trusted_evidence=evidence,
        sentinel_risk_evidence=risk,
        reservation=_reservation(),
    )
    checks = _by_prerequisite(report)
    assert checks[ProductionReadinessPrerequisite.SENTINEL_RISK_VERIFICATION].status is ProductionReadinessCheckStatus.BLOCKED
    assert checks[ProductionReadinessPrerequisite.DURABLE_RESERVATION].status is ProductionReadinessCheckStatus.BLOCKED
    assert "DURABLE_RESERVATION_TEST_ONLY" in report.reason_codes
    assert report.execution_authority is False


def test_real_registry_verification_is_read_only_and_does_not_remove_global_blocks() -> None:
    proposal, decision, registry, _intent = _pair()
    gate = ProductionAuthorizationReadinessGate(registry)
    first = gate.evaluate(now=NOW, proposal=proposal, decision=decision)
    second = gate.evaluate(now=NOW, proposal=proposal, decision=decision)
    check = _by_prerequisite(first)[ProductionReadinessPrerequisite.SENTINEL_TRADE_APPROVAL]

    assert check.status is ProductionReadinessCheckStatus.SATISFIED
    assert first == second
    assert first.production_authorization_available is False
    assert "PRODUCTION_EXECUTION_AUTHORIZATION_UNAVAILABLE" in first.reason_codes


@pytest.mark.parametrize("change", ({"expires_at": NOW}, {"proposal_id": "different"}))
def test_expired_or_mismatched_sentinel_approval_fails_closed(
    change: dict[str, object],
) -> None:
    proposal, decision, registry, _intent = _pair()
    report = ProductionAuthorizationReadinessGate(registry).evaluate(
        now=NOW,
        proposal=proposal,
        decision=decision.model_copy(update=change),
    )
    check = _by_prerequisite(report)[ProductionReadinessPrerequisite.SENTINEL_TRADE_APPROVAL]
    assert check.status is ProductionReadinessCheckStatus.INVALID
    assert check.reason_codes == ("DURABLE_SENTINEL_APPROVAL_INVALID",)


def test_protocol_shaped_registry_cannot_be_injected_as_authority() -> None:
    class FakeRegistry:
        def verify(self, **_kwargs):
            raise AssertionError("untrusted registry must never be called")

    proposal, decision, _registry, _intent = _pair()
    report = ProductionAuthorizationReadinessGate(FakeRegistry()).evaluate(  # type: ignore[arg-type]
        now=NOW,
        proposal=proposal,
        decision=decision,
    )
    check = _by_prerequisite(report)[ProductionReadinessPrerequisite.SENTINEL_TRADE_APPROVAL]
    assert check.status is ProductionReadinessCheckStatus.UNAVAILABLE


def test_audit_and_kill_switch_are_read_from_exact_concrete_dependencies(tmp_path) -> None:
    audit = AuditService(AuditStore(tmp_path / "audit.sqlite3"))
    switch = ExecutionKillSwitch()
    report = ProductionAuthorizationReadinessGate(
        audit_service=audit,
        kill_switch=switch,
    ).evaluate(now=NOW)
    checks = _by_prerequisite(report)
    assert checks[ProductionReadinessPrerequisite.AUDIT_DURABILITY].status is ProductionReadinessCheckStatus.SATISFIED
    assert checks[ProductionReadinessPrerequisite.KILL_SWITCH].status is ProductionReadinessCheckStatus.SATISFIED
    assert report.submission_allowed is False

    switch.trigger(reason="test", changed_by="pytest")
    blocked = ProductionAuthorizationReadinessGate(
        audit_service=audit,
        kill_switch=switch,
    ).evaluate(now=NOW)
    assert _by_prerequisite(blocked)[ProductionReadinessPrerequisite.KILL_SWITCH].status is ProductionReadinessCheckStatus.BLOCKED


def test_readiness_module_has_no_provider_or_submission_path() -> None:
    source = Path("app/services/production_authorization_readiness.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "MetaTrader5",
        "order_send",
        "MT5ExecutionAdapter",
        "PilotExecutionCoordinator",
        ".submit(",
    ):
        assert forbidden not in source


def test_local_durable_replay_is_reported_separately_from_external_rollback(
    tmp_path,
) -> None:
    replay = DurableIndependentReplayStore.initialize(tmp_path / "replay.sqlite3", now=NOW)
    report = ProductionAuthorizationReadinessGate(replay_store=replay).evaluate(now=NOW)
    checks = _by_prerequisite(report)

    assert checks[
        ProductionReadinessPrerequisite.INDEPENDENT_REPLAY_STORAGE
    ].status is ProductionReadinessCheckStatus.SATISFIED
    assert checks[
        ProductionReadinessPrerequisite.ATOMIC_REPLAY_CONSUMPTION
    ].status is ProductionReadinessCheckStatus.SATISFIED
    assert checks[
        ProductionReadinessPrerequisite.EXTERNAL_ROLLBACK_ASSURANCE
    ].status is ProductionReadinessCheckStatus.UNAVAILABLE
    assert "EXTERNAL_ROLLBACK_WITNESS_UNAVAILABLE" in report.reason_codes
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_protocol_shaped_replay_store_cannot_satisfy_readiness() -> None:
    class FakeReplayStore:
        def diagnostics(self, **_kwargs):
            raise AssertionError("untrusted replay store must not be called")

    report = ProductionAuthorizationReadinessGate(
        replay_store=FakeReplayStore(),  # type: ignore[arg-type]
    ).evaluate(now=NOW)
    checks = _by_prerequisite(report)
    assert checks[
        ProductionReadinessPrerequisite.INDEPENDENT_REPLAY_STORAGE
    ].status is ProductionReadinessCheckStatus.UNAVAILABLE
