"""Deterministic operational readiness; it never authorizes live execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.health.models import ComponentHealth, HealthStatus
from app.models.execution_state import ExecutionLifecycleState, ExecutionRecord
from app.services.config_validation import ConfigurationValidation


class ReadinessStatus(str, Enum):
    READY_FOR_DRY_RUN = "ready_for_dry_run"
    READY_FOR_CONTROLLED_VALIDATION = "ready_for_controlled_validation"
    NOT_READY = "not_ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: ReadinessStatus
    reason_codes: tuple[str, ...]
    live_execution_status: str = "LIVE_EXECUTION_BLOCKED"


class OperationalReadinessChecker:
    """Checks existing controls only; it performs no provider, broker, or MT5 calls."""

    def evaluate(
        self,
        *,
        configuration: ConfigurationValidation,
        health: tuple[ComponentHealth, ...],
        kill_switch_active: bool,
        execution_records: tuple[ExecutionRecord, ...],
        sentinel_available: bool,
        audit_available: bool,
        require_mt5: bool = False,
    ) -> ReadinessReport:
        codes: list[str] = list(configuration.reason_codes)
        if not sentinel_available:
            codes.append("SENTINEL_UNAVAILABLE")
        if not audit_available:
            codes.append("AUDIT_PERSISTENCE_UNAVAILABLE")
        if kill_switch_active:
            codes.append("KILL_SWITCH_ACTIVE")
        if any(record.state is ExecutionLifecycleState.RECONCILIATION_REQUIRED for record in execution_records):
            codes.append("UNRESOLVED_RECONCILIATION")
        by_component = {item.component: item for item in health}
        for required in ("audit_store", "sentinel", "execution_adapter"):
            item = by_component.get(required)
            if item is None or item.status is not HealthStatus.HEALTHY:
                codes.append(f"{required.upper()}_UNAVAILABLE")
        if require_mt5:
            item = by_component.get("mt5")
            if item is None or item.status is not HealthStatus.HEALTHY:
                codes.append("MT5_UNAVAILABLE")
        if codes:
            blocking = {"KILL_SWITCH_ACTIVE", "UNRESOLVED_RECONCILIATION", "LIVE_EXECUTION_BLOCKED"}
            status = ReadinessStatus.BLOCKED if any(code in blocking for code in codes) else ReadinessStatus.NOT_READY
            return ReadinessReport(status=status, reason_codes=tuple(sorted(set(codes))))
        if configuration.execution_mode == "dry_run":
            return ReadinessReport(status=ReadinessStatus.READY_FOR_DRY_RUN, reason_codes=())
        return ReadinessReport(status=ReadinessStatus.READY_FOR_CONTROLLED_VALIDATION, reason_codes=())
