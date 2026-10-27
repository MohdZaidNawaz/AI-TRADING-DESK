"""Fail-closed operational-readiness evaluation with no provider or execution I/O."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
import re

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
from app.services.durable_independent_replay import DurableIndependentReplayStore


class OfflineOperationalAcceptanceEvaluator:
    """Compose offline diagnostics while remaining permanently non-authorizing.

    Caller-provided claims are never trusted as verified observations. The
    only currently supported concrete observation is the exact durable replay
    store's read-only diagnostics. Unsupported controls remain unavailable or
    not assessed.
    """

    def __init__(
        self,
        *,
        replay_store: DurableIndependentReplayStore | None = None,
    ) -> None:
        self._replay_store = (
            replay_store
            if type(replay_store) is DurableIndependentReplayStore
            else None
        )

    def evaluate(
        self,
        *,
        now: datetime,
        evidence: tuple[OperationalReadinessEvidence, ...] = (),
        configuration: OperationalRuntimeConfiguration | None = None,
    ) -> OperationalReadinessReport:
        evaluated_at = _utc(now)
        grouped: dict[
            OperationalReadinessPrerequisite, list[OperationalReadinessEvidence]
        ] = defaultdict(list)
        for item in evidence:
            grouped[item.prerequisite].append(item)

        checks = tuple(
            sorted(
                (
                    self._evaluate_one(
                        prerequisite,
                        grouped[prerequisite],
                        evaluated_at,
                        configuration,
                    )
                    for prerequisite in OperationalReadinessPrerequisite
                ),
                key=lambda check: check.prerequisite.value,
            )
        )
        reasons = tuple(
            sorted(
                {
                    code
                    for check in checks
                    if check.status is not OperationalReadinessStatus.SATISFIED
                    for code in check.reason_codes
                }
            )
        )
        return OperationalReadinessReport(
            evaluated_at=evaluated_at,
            checks=checks,
            reason_codes=reasons,
        )

    def _evaluate_one(
        self,
        prerequisite: OperationalReadinessPrerequisite,
        items: list[OperationalReadinessEvidence],
        now: datetime,
        configuration: OperationalRuntimeConfiguration | None,
    ) -> OperationalReadinessCheck:
        if prerequisite is OperationalReadinessPrerequisite.APPLICATION_CONFIGURATION:
            if configuration is not None and items:
                return _failed(
                    prerequisite,
                    OperationalReadinessStatus.INVALID,
                    "CONFLICTING_CONFIGURATION_ASSESSMENTS",
                    now,
                )
            if configuration is not None:
                return OperationalReadinessCheck(
                    prerequisite=prerequisite,
                    status=OperationalReadinessStatus.BLOCKED,
                    reason_codes=(
                        "CONFIGURATION_POLICY_VALID_RUNTIME_UNVERIFIED",
                    ),
                    source_reference="phase20-structural-policy-validation",
                    evidence_fingerprint=configuration.fingerprint,
                    checked_at=now,
                )
        if prerequisite is OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE:
            if self._replay_store is not None and items:
                return _failed(
                    prerequisite,
                    OperationalReadinessStatus.INVALID,
                    "CONFLICTING_REPLAY_ASSESSMENTS",
                    now,
                )
            if self._replay_store is not None:
                return self._replay_check(now)
        if not items:
            return _failed(
                prerequisite,
                OperationalReadinessStatus.NOT_ASSESSED,
                "OPERATIONAL_PREREQUISITE_NOT_ASSESSED",
                now,
            )
        if len(items) != 1:
            return _failed(
                prerequisite,
                OperationalReadinessStatus.INVALID,
                "CONFLICTING_OPERATIONAL_EVIDENCE",
                now,
            )

        item = items[0]
        if item.assessed_at > now:
            return _failed(
                prerequisite,
                OperationalReadinessStatus.INVALID,
                "OPERATIONAL_EVIDENCE_FUTURE_DATED",
                now,
            )
        if item.expires_at is not None and item.expires_at <= now:
            return _failed(
                prerequisite,
                OperationalReadinessStatus.STALE,
                "OPERATIONAL_EVIDENCE_STALE",
                now,
                item,
            )
        if item.status is not OperationalReadinessStatus.SATISFIED:
            return _failed_many(prerequisite, item.status, item.reason_codes, now, item)
        if item.source is OperationalEvidenceSource.SYNTHETIC_TEST:
            return _failed(
                prerequisite,
                OperationalReadinessStatus.BLOCKED,
                "SYNTHETIC_EVIDENCE_NOT_OPERATIONAL",
                now,
                item,
            )
        return _failed(
            prerequisite,
            OperationalReadinessStatus.BLOCKED,
            "CALLER_ASSERTED_EVIDENCE_NOT_TRUSTED",
            now,
            item,
        )

    def _replay_check(self, now: datetime) -> OperationalReadinessCheck:
        assert self._replay_store is not None
        try:
            diagnostics = DurableIndependentReplayStore.diagnostics(
                self._replay_store,
                checked_at=now,
            )
        except Exception:
            return _failed(
                OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                OperationalReadinessStatus.UNAVAILABLE,
                "REPLAY_DIAGNOSTICS_UNAVAILABLE",
                now,
            )
        if type(diagnostics) is not IndependentReplayStoreDiagnostics:
            return _failed(
                OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                OperationalReadinessStatus.INVALID,
                "REPLAY_DIAGNOSTICS_INVALID",
                now,
            )
        try:
            checked_at = diagnostics.checked_at
            fingerprint = diagnostics.fingerprint
            canonical = IndependentReplayStoreDiagnostics.model_validate(
                diagnostics.model_dump(exclude={"fingerprint"})
            )
            canonical_fingerprint = canonical.fingerprint
        except Exception:
            return _failed(
                OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                OperationalReadinessStatus.INVALID,
                "REPLAY_DIAGNOSTICS_INVALID",
                now,
            )
        if checked_at != now:
            return _failed(
                OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                (
                    OperationalReadinessStatus.INVALID
                    if checked_at > now
                    else OperationalReadinessStatus.STALE
                ),
                (
                    "REPLAY_DIAGNOSTICS_FUTURE_DATED"
                    if checked_at > now
                    else "REPLAY_DIAGNOSTICS_STALE"
                ),
                now,
            )
        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            return _failed(
                OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                OperationalReadinessStatus.INVALID,
                "REPLAY_DIAGNOSTICS_FINGERPRINT_INVALID",
                now,
            )
        if canonical_fingerprint != fingerprint:
            return _failed(
                OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                OperationalReadinessStatus.INVALID,
                "REPLAY_DIAGNOSTICS_FINGERPRINT_MISMATCH",
                now,
            )
        diagnostics = canonical
        if diagnostics.status is IndependentReplayStoreStatus.READY:
            assert diagnostics.store_id is not None
            return OperationalReadinessCheck(
                prerequisite=OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
                status=OperationalReadinessStatus.SATISFIED,
                reason_codes=(),
                source_reference=f"durable-independent-replay:{diagnostics.store_id}",
                evidence_fingerprint=fingerprint,
                checked_at=now,
            )
        status = (
            OperationalReadinessStatus.UNAVAILABLE
            if diagnostics.status
            in {
                IndependentReplayStoreStatus.MISSING,
                IndependentReplayStoreStatus.UNAVAILABLE,
            }
            else OperationalReadinessStatus.INVALID
        )
        return OperationalReadinessCheck(
            prerequisite=OperationalReadinessPrerequisite.DURABLE_REPLAY_STORAGE,
            status=status,
            reason_codes=diagnostics.reason_codes,
            checked_at=now,
        )


def _failed(
    prerequisite: OperationalReadinessPrerequisite,
    status: OperationalReadinessStatus,
    reason: str,
    now: datetime,
    evidence: OperationalReadinessEvidence | None = None,
) -> OperationalReadinessCheck:
    return _failed_many(prerequisite, status, (reason,), now, evidence)


def _failed_many(
    prerequisite: OperationalReadinessPrerequisite,
    status: OperationalReadinessStatus,
    reasons: tuple[str, ...],
    now: datetime,
    evidence: OperationalReadinessEvidence | None = None,
) -> OperationalReadinessCheck:
    return OperationalReadinessCheck(
        prerequisite=prerequisite,
        status=status,
        reason_codes=tuple(sorted(set(reasons))),
        source_reference=evidence.source_reference if evidence is not None else None,
        evidence_fingerprint=evidence.evidence_fingerprint if evidence is not None else None,
        checked_at=now,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operational readiness evaluation requires timezone awareness")
    return value.astimezone(UTC)
