"""Phase 20 operational-readiness and observability tests."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from enum import Enum
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.operational_readiness import (
    OperationalEvidenceSource,
    OperationalReadinessCheck,
    OperationalReadinessEvidence,
    OperationalReadinessPrerequisite,
    OperationalReadinessReport,
    OperationalReadinessStatus,
    OperationalRuntimeConfiguration,
)
from app.models.independent_replay import (
    IndependentReplayStoreDiagnostics,
    IndependentReplayStoreStatus,
)
from app.observability.events import (
    OperationalEventState,
    OperationalReasonCode,
    OperationalSignal,
    log_event,
    operational_event,
)
from app.services.durable_independent_replay import DurableIndependentReplayStore
from app.services.operational_readiness import OfflineOperationalAcceptanceEvaluator


NOW = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _evidence(
    prerequisite: OperationalReadinessPrerequisite,
    *,
    status: OperationalReadinessStatus = OperationalReadinessStatus.SATISFIED,
    source: OperationalEvidenceSource = OperationalEvidenceSource.LOCAL_CLAIM,
    assessed_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime | None = NOW + timedelta(minutes=5),
    reason_codes: tuple[str, ...] = (),
) -> OperationalReadinessEvidence:
    return OperationalReadinessEvidence(
        prerequisite=prerequisite,
        status=status,
        source=source,
        source_reference="sanitized-phase20-evidence",
        evidence_fingerprint=FINGERPRINT,
        assessed_at=assessed_at,
        expires_at=expires_at,
        reason_codes=reason_codes,
    )


def _checks(report: OperationalReadinessReport):
    return {check.prerequisite: check for check in report.checks}


def _configuration(**changes) -> OperationalRuntimeConfiguration:
    values = {
        "configuration_revision": "b" * 64,
        "execution_mode": "disabled",
        "provider_connect_timeout_seconds": 1,
        "provider_read_timeout_seconds": 2,
        "provider_overall_timeout_seconds": 2,
        "provider_maximum_concurrency": 4,
        "maximum_clock_skew_seconds": 30,
        "freshness_max_age_seconds": 300,
    }
    values.update(changes)
    return OperationalRuntimeConfiguration(**values)


def _ready_replay_diagnostics(
    *, checked_at: datetime = NOW
) -> IndependentReplayStoreDiagnostics:
    return IndependentReplayStoreDiagnostics(
        status=IndependentReplayStoreStatus.READY,
        checked_at=checked_at,
        database_schema_version=2,
        store_id="c" * 32,
        durable_across_restart=True,
        atomic_consumption=True,
    )


def _assert_replay_failed_closed(report: OperationalReadinessReport) -> None:
    checks = _checks(report)
    assert checks[
        OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE
    ].status is not OperationalReadinessStatus.SATISFIED
    assert checks[
        OperationalReadinessPrerequisite.EXTERNAL_ROLLBACK_WITNESS
    ].status is not OperationalReadinessStatus.SATISFIED
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_empty_assessment_is_complete_blocked_and_non_authorizing() -> None:
    report = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW)

    assert report.status == "blocked"
    assert len(report.checks) == len(OperationalReadinessPrerequisite)
    assert {check.status for check in report.checks} == {
        OperationalReadinessStatus.NOT_ASSESSED
    }
    assert report.production_authorization_available is False
    assert report.submission_allowed is False
    assert report.execution_authority is False
    assert all(check.authorizes_execution is False for check in report.checks)


def test_caller_created_local_claim_cannot_satisfy_a_prerequisite() -> None:
    local = _evidence(OperationalReadinessPrerequisite.APPLICATION_CONFIGURATION)
    report = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=(local,))

    check = _checks(report)[local.prerequisite]
    assert check.status is OperationalReadinessStatus.BLOCKED
    assert check.reason_codes == ("CALLER_ASSERTED_EVIDENCE_NOT_TRUSTED",)
    assert report.status == "blocked"
    assert report.execution_authority is False


def test_bounded_policy_is_structurally_valid_but_runtime_remains_unverified() -> None:
    report = OfflineOperationalAcceptanceEvaluator().evaluate(
        now=NOW, configuration=_configuration()
    )
    checks = _checks(report)
    configuration = checks[OperationalReadinessPrerequisite.APPLICATION_CONFIGURATION]
    assert configuration.status is OperationalReadinessStatus.BLOCKED
    assert configuration.reason_codes == (
        "CONFIGURATION_POLICY_VALID_RUNTIME_UNVERIFIED",
    )
    assert configuration.evidence_fingerprint is not None
    for prerequisite in (
        OperationalReadinessPrerequisite.CLOCK_AND_FRESHNESS,
        OperationalReadinessPrerequisite.PROVIDER_LIMITS,
        OperationalReadinessPrerequisite.EXECUTION_CONTROLS,
    ):
        assert checks[prerequisite].status is OperationalReadinessStatus.NOT_ASSESSED
    assert checks[
        OperationalReadinessPrerequisite.INDEPENDENT_TRUST_ROOT
    ].status is OperationalReadinessStatus.NOT_ASSESSED
    assert report.production_authorization_available is False


@pytest.mark.parametrize(
    "change",
    (
        {"execution_mode": "live"},
        {"provider_connect_timeout_seconds": 0},
        {"provider_read_timeout_seconds": 31},
        {"provider_overall_timeout_seconds": 1},
        {"provider_maximum_concurrency": 0},
        {"provider_maximum_concurrency": 33},
        {"maximum_clock_skew_seconds": 301},
        {"freshness_max_age_seconds": 0},
    ),
)
def test_invalid_execution_timeout_capacity_and_freshness_config_fails_construction(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _configuration(**change)


@pytest.mark.parametrize(
    "prerequisite",
    (
        OperationalReadinessPrerequisite.INDEPENDENT_TRUST_ROOT,
        OperationalReadinessPrerequisite.LIFECYCLE_REVOCATION,
        OperationalReadinessPrerequisite.EXTERNAL_ROLLBACK_WITNESS,
        OperationalReadinessPrerequisite.BROKER_EVIDENCE,
        OperationalReadinessPrerequisite.AUTHORITATIVE_RECONCILIATION,
        OperationalReadinessPrerequisite.MONITORING_AND_ALERTING,
        OperationalReadinessPrerequisite.INDEPENDENT_SECURITY_REVIEW,
        OperationalReadinessPrerequisite.HUMAN_PRODUCTION_APPROVAL,
    ),
)
def test_model_shaped_external_or_manual_success_cannot_establish_authority(
    prerequisite: OperationalReadinessPrerequisite,
) -> None:
    claim = _evidence(
        prerequisite,
        source=(
            OperationalEvidenceSource.MANUAL_CLAIM
            if prerequisite
            in {
                OperationalReadinessPrerequisite.INDEPENDENT_SECURITY_REVIEW,
                OperationalReadinessPrerequisite.HUMAN_PRODUCTION_APPROVAL,
            }
            else OperationalEvidenceSource.EXTERNAL_CLAIM
        ),
    )
    report = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=(claim,))
    check = _checks(report)[prerequisite]

    assert check.status is OperationalReadinessStatus.BLOCKED
    assert check.reason_codes == ("CALLER_ASSERTED_EVIDENCE_NOT_TRUSTED",)
    assert report.production_authorization_available is False


def test_synthetic_success_never_satisfies_even_local_prerequisite() -> None:
    synthetic = _evidence(
        OperationalReadinessPrerequisite.EXECUTION_CONTROLS,
        source=OperationalEvidenceSource.SYNTHETIC_TEST,
    )
    check = _checks(
        OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=(synthetic,))
    )[synthetic.prerequisite]
    assert check.status is OperationalReadinessStatus.BLOCKED
    assert check.reason_codes == ("SYNTHETIC_EVIDENCE_NOT_OPERATIONAL",)


def test_readiness_transition_keeps_local_and_synthetic_claims_blocked() -> None:
    prerequisite = OperationalReadinessPrerequisite.EXECUTION_CONTROLS
    local = _evidence(prerequisite)
    synthetic = _evidence(prerequisite, source=OperationalEvidenceSource.SYNTHETIC_TEST)

    before = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=(local,))
    after = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=(synthetic,))

    assert _checks(before)[prerequisite].status is OperationalReadinessStatus.BLOCKED
    assert _checks(after)[prerequisite].status is OperationalReadinessStatus.BLOCKED
    assert after.production_authorization_available is False


def test_required_operational_failures_remain_distinct_and_fail_closed() -> None:
    evidence = (
        _evidence(
            OperationalReadinessPrerequisite.EXTERNAL_ROLLBACK_WITNESS,
            status=OperationalReadinessStatus.BLOCKED,
            source=OperationalEvidenceSource.EXTERNAL_CLAIM,
            reason_codes=("WITNESS_CHECKPOINT_CONFLICT",),
        ),
        _evidence(
            OperationalReadinessPrerequisite.BROKER_EVIDENCE,
            status=OperationalReadinessStatus.INVALID,
            source=OperationalEvidenceSource.EXTERNAL_CLAIM,
            reason_codes=("BROKER_EVIDENCE_INCOMPLETE",),
        ),
        _evidence(
            OperationalReadinessPrerequisite.AUTHORITATIVE_RECONCILIATION,
            status=OperationalReadinessStatus.UNAVAILABLE,
            source=OperationalEvidenceSource.EXTERNAL_CLAIM,
            reason_codes=("RECONCILIATION_UNRESOLVED",),
        ),
        _evidence(
            OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
            status=OperationalReadinessStatus.INVALID,
            reason_codes=("REPLAY_STORE_UNHEALTHY",),
        ),
        _evidence(
            OperationalReadinessPrerequisite.MONITORING_AND_ALERTING,
            status=OperationalReadinessStatus.UNAVAILABLE,
            reason_codes=("ALERT_DELIVERY_UNAVAILABLE",),
        ),
        _evidence(
            OperationalReadinessPrerequisite.BACKUP_RECOVERY_INCIDENT_RESPONSE,
            status=OperationalReadinessStatus.NOT_ASSESSED,
            reason_codes=("RECOVERY_REHEARSAL_NOT_ASSESSED",),
        ),
        _evidence(
            OperationalReadinessPrerequisite.INDEPENDENT_SECURITY_REVIEW,
            status=OperationalReadinessStatus.STALE,
            source=OperationalEvidenceSource.MANUAL_CLAIM,
            reason_codes=("SECURITY_REVIEW_STALE",),
        ),
    )
    report = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=evidence)
    checks = _checks(report)

    assert checks[
        OperationalReadinessPrerequisite.EXTERNAL_ROLLBACK_WITNESS
    ].status is OperationalReadinessStatus.BLOCKED
    assert checks[
        OperationalReadinessPrerequisite.BROKER_EVIDENCE
    ].status is OperationalReadinessStatus.INVALID
    assert checks[
        OperationalReadinessPrerequisite.AUTHORITATIVE_RECONCILIATION
    ].status is OperationalReadinessStatus.UNAVAILABLE
    assert checks[
        OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE
    ].status is OperationalReadinessStatus.INVALID
    assert checks[
        OperationalReadinessPrerequisite.INDEPENDENT_SECURITY_REVIEW
    ].status is OperationalReadinessStatus.STALE
    assert report.submission_allowed is False
    assert report.execution_authority is False


def test_stale_future_and_conflicting_evidence_fail_closed() -> None:
    prerequisite = OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE
    stale = _evidence(
        prerequisite,
        assessed_at=NOW - timedelta(minutes=5),
        expires_at=NOW,
    )
    future = _evidence(
        OperationalReadinessPrerequisite.CLOCK_AND_FRESHNESS,
        assessed_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    duplicate = _evidence(prerequisite)
    report = OfflineOperationalAcceptanceEvaluator().evaluate(
        now=NOW, evidence=(stale, duplicate, future)
    )
    checks = _checks(report)
    assert checks[prerequisite].status is OperationalReadinessStatus.INVALID
    assert checks[future.prerequisite].status is OperationalReadinessStatus.INVALID


@pytest.mark.parametrize(
    ("status", "code"),
    (
        (OperationalReadinessStatus.BLOCKED, "DEPENDENCY_BLOCKED"),
        (OperationalReadinessStatus.INVALID, "DEPENDENCY_INVALID"),
        (OperationalReadinessStatus.STALE, "DEPENDENCY_STALE"),
        (OperationalReadinessStatus.UNAVAILABLE, "DEPENDENCY_UNAVAILABLE"),
        (OperationalReadinessStatus.NOT_ASSESSED, "DEPENDENCY_NOT_ASSESSED"),
    ),
)
def test_dependency_failure_status_is_preserved(
    status: OperationalReadinessStatus, code: str
) -> None:
    item = _evidence(
        OperationalReadinessPrerequisite.BROKER_EVIDENCE,
        status=status,
        source=OperationalEvidenceSource.EXTERNAL_CLAIM,
        reason_codes=(code,),
    )
    check = _checks(
        OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW, evidence=(item,))
    )[item.prerequisite]
    assert check.status is status
    assert check.reason_codes == (code,)


def test_evidence_rejects_secret_reference_and_malformed_chronology() -> None:
    with pytest.raises(ValidationError):
        _evidence(OperationalReadinessPrerequisite.APPLICATION_CONFIGURATION).model_copy(
            update={"source_reference": "token=synthetic-sensitive"}
        ).__class__.model_validate(
            {
                **_evidence(
                    OperationalReadinessPrerequisite.APPLICATION_CONFIGURATION
                ).model_dump(),
                "source_reference": "token=synthetic-sensitive",
            }
        )
    with pytest.raises(ValidationError):
        _evidence(
            OperationalReadinessPrerequisite.APPLICATION_CONFIGURATION,
            assessed_at=NOW,
            expires_at=NOW,
        )


def test_satisfied_check_requires_fingerprinted_provenance() -> None:
    with pytest.raises(ValidationError, match="fingerprinted provenance"):
        OperationalReadinessCheck(
            prerequisite=OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
            status=OperationalReadinessStatus.SATISFIED,
            reason_codes=(),
            source_reference="durable-replay",
            checked_at=NOW,
        )
    with pytest.raises(ValidationError):
        OperationalReadinessCheck(
            prerequisite=OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
            status=OperationalReadinessStatus.SATISFIED,
            reason_codes=(),
            source_reference="token=synthetic-sensitive",
            evidence_fingerprint="not-a-fingerprint",
            checked_at=NOW,
        )


def test_exact_durable_replay_store_diagnostics_can_satisfy_local_store_health(
    tmp_path,
) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )
    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)
    check = _checks(report)[OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE]

    assert check.status is OperationalReadinessStatus.SATISFIED
    assert check.evidence_fingerprint is not None
    assert check.source_reference is not None
    assert check.source_reference.startswith("durable-independent-replay:")
    assert report.status == "blocked"
    assert report.production_authorization_available is False


def test_protocol_shaped_replay_store_is_not_called_or_trusted() -> None:
    class FakeReplayStore:
        def diagnostics(self, **_kwargs):
            raise AssertionError("caller-provided replay diagnostic must not be called")

    report = OfflineOperationalAcceptanceEvaluator(
        replay_store=FakeReplayStore(),  # type: ignore[arg-type]
    ).evaluate(now=NOW)
    check = _checks(report)[OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE]
    assert check.status is OperationalReadinessStatus.NOT_ASSESSED


def test_exact_store_instance_shadowed_diagnostics_is_never_invoked(tmp_path) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )
    invoked = False

    def forged(**_kwargs):
        nonlocal invoked
        invoked = True
        return _ready_replay_diagnostics()

    store.diagnostics = forged  # type: ignore[method-assign]
    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)

    assert invoked is False
    assert _checks(report)[
        OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE
    ].status is OperationalReadinessStatus.SATISFIED
    assert report.execution_authority is False


@pytest.mark.parametrize("returned", ({"status": "ready"}, object()))
def test_unexpected_or_model_shaped_replay_diagnostics_fail_closed(
    tmp_path, monkeypatch, returned
) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )
    monkeypatch.setattr(
        DurableIndependentReplayStore,
        "diagnostics",
        lambda _self, *, checked_at: returned,
    )

    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)
    _assert_replay_failed_closed(report)
    assert "REPLAY_DIAGNOSTICS_INVALID" in report.reason_codes


def test_exact_type_with_malformed_replay_fields_fails_closed(
    tmp_path, monkeypatch
) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )
    malformed = IndependentReplayStoreDiagnostics.model_construct(
        status=IndependentReplayStoreStatus.READY,
        checked_at=NOW,
    )
    monkeypatch.setattr(
        DurableIndependentReplayStore,
        "diagnostics",
        lambda _self, *, checked_at: malformed,
    )

    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)
    _assert_replay_failed_closed(report)
    assert "REPLAY_DIAGNOSTICS_INVALID" in report.reason_codes


@pytest.mark.parametrize(
    ("checked_at", "expected"),
    (
        (NOW - timedelta(seconds=1), OperationalReadinessStatus.STALE),
        (NOW + timedelta(seconds=1), OperationalReadinessStatus.INVALID),
    ),
)
def test_stale_or_future_replay_diagnostics_fail_closed(
    tmp_path, monkeypatch, checked_at, expected
) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )
    diagnostics = _ready_replay_diagnostics(checked_at=checked_at)
    monkeypatch.setattr(
        DurableIndependentReplayStore,
        "diagnostics",
        lambda _self, *, checked_at: diagnostics,
    )

    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)
    _assert_replay_failed_closed(report)
    assert _checks(report)[
        OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE
    ].status is expected


def test_replay_diagnostic_exception_fails_closed_without_message(
    tmp_path, monkeypatch
) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )

    def fail(_self, *, checked_at):
        raise RuntimeError("token=synthetic-sensitive")

    monkeypatch.setattr(DurableIndependentReplayStore, "diagnostics", fail)
    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)

    _assert_replay_failed_closed(report)
    rendered = str(report.model_dump(mode="json"))
    assert "synthetic-sensitive" not in rendered
    assert "REPLAY_DIAGNOSTICS_UNAVAILABLE" in report.reason_codes


@pytest.mark.parametrize("fingerprint", (None, "invalid-fingerprint"))
def test_missing_or_invalid_replay_diagnostic_fingerprint_fails_closed(
    tmp_path, monkeypatch, fingerprint
) -> None:
    store = DurableIndependentReplayStore.initialize(
        tmp_path / "replay.sqlite3", now=NOW
    )
    diagnostics = _ready_replay_diagnostics()
    monkeypatch.setattr(
        DurableIndependentReplayStore,
        "diagnostics",
        lambda _self, *, checked_at: diagnostics,
    )
    monkeypatch.setattr(
        IndependentReplayStoreDiagnostics,
        "fingerprint",
        property(lambda _self: fingerprint),
    )

    report = OfflineOperationalAcceptanceEvaluator(replay_store=store).evaluate(now=NOW)
    _assert_replay_failed_closed(report)
    assert "REPLAY_DIAGNOSTICS_FINGERPRINT_INVALID" in report.reason_codes


def test_report_flags_reject_true_and_report_is_immutable() -> None:
    report = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW)
    for name in (
        "production_authorization_available",
        "submission_allowed",
        "execution_authority",
    ):
        with pytest.raises(ValidationError):
            OperationalReadinessReport.model_validate(
                {**report.model_dump(), name: True}
            )
    with pytest.raises(ValidationError):
        report.status = "ready"  # type: ignore[misc]


def test_fingerprint_and_ordering_are_deterministic() -> None:
    first = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW)
    second = OfflineOperationalAcceptanceEvaluator().evaluate(now=NOW)
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert tuple(check.prerequisite.value for check in first.checks) == tuple(
        sorted(check.prerequisite.value for check in first.checks)
    )


def test_operational_signal_is_bounded_and_rejects_secret_looking_labels(caplog) -> None:
    event = operational_event(
        timestamp=NOW,
        signal=OperationalSignal.CONFIGURATION_FAILURE,
        state=OperationalEventState.BLOCKED,
        reason_code=OperationalReasonCode.CONFIGURATION_INVALID,
        severity="ERROR",
    )
    caplog.set_level(logging.INFO, logger="friday")
    log_event(event)
    assert "CONFIGURATION_INVALID" in caplog.text
    with pytest.raises(ValueError, match="bounded vocabulary"):
        operational_event(
            timestamp=NOW,
            signal=OperationalSignal.CONFIGURATION_FAILURE,
            state="blocked",  # type: ignore[arg-type]
            reason_code=OperationalReasonCode.CONFIGURATION_INVALID,
        )
    with pytest.raises(ValueError, match="bounded vocabulary"):
        operational_event(
            timestamp=NOW,
            signal=OperationalSignal.CONFIGURATION_FAILURE,
            state=OperationalEventState.BLOCKED,
            reason_code="CONFIGURATION_INVALID",  # type: ignore[arg-type]
        )
    assert "synthetic-sensitive" not in caplog.text


@pytest.mark.parametrize(
    "signal",
    (
        "configuration_failure",
        object(),
    ),
)
def test_plain_or_arbitrary_operational_signal_is_rejected(signal) -> None:
    with pytest.raises(ValueError, match="signal must use the bounded vocabulary"):
        operational_event(
            timestamp=NOW,
            signal=signal,  # type: ignore[arg-type]
            state=OperationalEventState.BLOCKED,
            reason_code=OperationalReasonCode.CONFIGURATION_INVALID,
        )


def test_foreign_enum_and_value_object_cannot_create_event_types() -> None:
    class ForeignSignal(str, Enum):
        ARBITRARY = "arbitrary_high_cardinality_value"

    class SignalLike:
        value = "arbitrary_high_cardinality_value"

    for signal in (ForeignSignal.ARBITRARY, SignalLike()):
        with pytest.raises(ValueError, match="signal must use the bounded vocabulary"):
            operational_event(
                timestamp=NOW,
                signal=signal,  # type: ignore[arg-type]
                state=OperationalEventState.BLOCKED,
                reason_code=OperationalReasonCode.CONFIGURATION_INVALID,
            )


def test_readiness_service_ast_has_no_provider_network_or_execution_path() -> None:
    source = Path("app/services/operational_readiness.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not imports.intersection({"MetaTrader5", "requests", "httpx", "socket"})
    assert not names.intersection(
        {
            "MetaTrader5",
            "order_send",
            "PilotExecutionCoordinator",
            "FinalExecutionGate",
            "submit",
        }
    )
