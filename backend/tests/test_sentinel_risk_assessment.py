"""Phase 7A tests for non-authorizing Sentinel risk-assessment contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.risk_assessment import (
    RiskCheckInputReference,
    RiskCheckResult,
    RiskCheckStatus,
    SentinelRiskAssessment,
)


MOMENT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
POLICY_FINGERPRINT = "a" * 64
PROPOSAL_FINGERPRINT = "b" * 64
EVIDENCE_FINGERPRINT = "c" * 64


def _reference() -> RiskCheckInputReference:
    return RiskCheckInputReference(
        reference_type="proposal",
        reference_id="proposal-1",
        fingerprint=PROPOSAL_FINGERPRINT,
    )


def _check(
    *,
    name: str = "proposal_parameters",
    status: RiskCheckStatus = RiskCheckStatus.PASS,
    reason_codes: tuple[str, ...] = ("validated",),
) -> RiskCheckResult:
    return RiskCheckResult(
        check_name=name,
        status=status,
        reason_codes=reason_codes,
        reason="A deterministic factual check result.",
        input_references=(_reference(),),
    )


def _assessment(
    *,
    checks: tuple[RiskCheckResult, ...] | None = None,
    policy_fingerprint: str = POLICY_FINGERPRINT,
    proposal_fingerprint: str = PROPOSAL_FINGERPRINT,
    evidence_fingerprint: str = EVIDENCE_FINGERPRINT,
) -> SentinelRiskAssessment:
    values = checks or (_check(),)
    overall = (
        RiskCheckStatus.REJECTED
        if any(item.status is RiskCheckStatus.REJECTED for item in values)
        else RiskCheckStatus.INSUFFICIENT_DATA
        if any(item.status is RiskCheckStatus.INSUFFICIENT_DATA for item in values)
        else RiskCheckStatus.PASS
    )
    return SentinelRiskAssessment(
        policy_id="sentinel-policy",
        policy_version="v1",
        policy_fingerprint=policy_fingerprint,
        proposal_id="proposal-1",
        proposal_fingerprint=proposal_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
        evidence_references=(_reference(),),
        checks=values,
        evaluated_at=MOMENT,
        overall_status=overall,
    )


def test_risk_check_is_immutable_and_has_deterministic_identity() -> None:
    first = _check()
    same = _check(reason_codes=("validated",))
    changed = _check(status=RiskCheckStatus.REJECTED, reason_codes=("invalid",))

    assert first.check_id == same.check_id
    assert first.fingerprint == same.fingerprint
    assert first.check_id == changed.check_id
    assert first.fingerprint != changed.fingerprint
    with pytest.raises(ValidationError):
        first.status = RiskCheckStatus.REJECTED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        first.input_references[0].reference_id = "changed"  # type: ignore[misc]


def test_risk_check_statuses_are_closed_and_provider_neutral() -> None:
    assert {item.value for item in RiskCheckStatus} == {
        "pass",
        "rejected",
        "insufficient_data",
        "not_applicable",
    }
    with pytest.raises(ValidationError):
        RiskCheckResult(check_name="raw", status="approved", raw_payload={})  # type: ignore[arg-type,call-arg]


def test_assessment_is_immutable_and_order_independent() -> None:
    first = _assessment(checks=(_check(name="market"), _check(name="parameters")))
    reordered = _assessment(checks=(_check(name="parameters"), _check(name="market")))

    assert first.assessment_id == reordered.assessment_id
    assert first.fingerprint == reordered.fingerprint
    assert tuple(item.check_name for item in first.checks) == ("market", "parameters")
    with pytest.raises(ValidationError):
        first.checks = ()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        first.checks[0].reason_codes = ("changed",)  # type: ignore[misc]


def test_assessment_fingerprint_changes_with_meaningful_inputs() -> None:
    baseline = _assessment()
    changed_check = _assessment(checks=(_check(status=RiskCheckStatus.INSUFFICIENT_DATA),))
    changed_policy = _assessment(policy_fingerprint="d" * 64)
    changed_proposal = _assessment(proposal_fingerprint="e" * 64)
    changed_evidence = _assessment(evidence_fingerprint="f" * 64)

    assert baseline.fingerprint != changed_check.fingerprint
    assert baseline.fingerprint != changed_policy.fingerprint
    assert baseline.fingerprint != changed_proposal.fingerprint
    assert baseline.fingerprint != changed_evidence.fingerprint


def test_assessment_validates_overall_status_and_duplicate_check_identity() -> None:
    values = _assessment().model_dump(mode="python", exclude={"overall_status", "assessment_id", "fingerprint"})
    with pytest.raises(ValidationError):
        SentinelRiskAssessment(**values, overall_status=RiskCheckStatus.REJECTED)
    with pytest.raises(ValidationError):
        _assessment(checks=(_check(), _check()))


def test_assessment_has_no_approval_execution_or_trade_instruction_fields() -> None:
    fields = set(SentinelRiskAssessment.model_fields)
    forbidden = {"approval_id", "execute", "direction", "entry", "stop_loss", "target", "sizing"}
    assert not fields.intersection(forbidden)
    assert not hasattr(_assessment(), "approve")
    assert not hasattr(_assessment(), "execute")
