from datetime import UTC, datetime

from app.health.models import ComponentHealth, HealthStatus
from app.services.config_validation import validate_runtime_configuration
from app.services.readiness import OperationalReadinessChecker, ReadinessStatus


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _health(component: str, status: HealthStatus = HealthStatus.HEALTHY) -> ComponentHealth:
    return ComponentHealth(component=component, status=status, checked_at=NOW)


def test_readiness_allows_controlled_dry_run_but_never_live() -> None:
    config = validate_runtime_configuration({"MT5_EXECUTION_MODE": "dry_run", "FRIDAY_AUDIT_DB_PATH": "audit.sqlite3"})
    result = OperationalReadinessChecker().evaluate(
        configuration=config, health=(_health("audit_store"), _health("sentinel"), _health("execution_adapter")),
        kill_switch_active=False, execution_records=(), sentinel_available=True, audit_available=True,
    )
    assert result.status is ReadinessStatus.READY_FOR_DRY_RUN
    assert result.live_execution_status == "LIVE_EXECUTION_BLOCKED"


def test_readiness_fails_closed_for_missing_mt5_or_kill_switch() -> None:
    config = validate_runtime_configuration({"MT5_EXECUTION_MODE": "disabled", "FRIDAY_AUDIT_DB_PATH": "audit.sqlite3"})
    result = OperationalReadinessChecker().evaluate(
        configuration=config, health=(_health("audit_store"), _health("sentinel"), _health("execution_adapter")),
        kill_switch_active=True, execution_records=(), sentinel_available=True, audit_available=True, require_mt5=True,
    )
    assert result.status is ReadinessStatus.BLOCKED
    assert {"KILL_SWITCH_ACTIVE", "MT5_UNAVAILABLE"}.issubset(result.reason_codes)
