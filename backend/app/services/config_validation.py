"""Fail-closed validation of non-secret operational configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import AUDIT_DB_PATH_ENV, MT5_EXECUTION_MODE_ENV, OPERATION_TIMEOUT_SECONDS_ENV, get_runtime_settings


@dataclass(frozen=True, slots=True)
class ConfigurationValidation:
    valid: bool
    execution_mode: str
    audit_path: Path | None
    operation_timeout_seconds: float | None
    reason_codes: tuple[str, ...]


def validate_runtime_configuration(settings: dict[str, str | None] | None = None) -> ConfigurationValidation:
    values = settings if settings is not None else get_runtime_settings()
    mode = (values.get(MT5_EXECUTION_MODE_ENV) or "disabled").strip().lower()
    reasons: list[str] = []
    if mode not in {"disabled", "read_only", "dry_run", "live"}:
        reasons.append("INVALID_EXECUTION_MODE")
    # This phase deliberately has no production live authorization path.
    if mode == "live":
        reasons.append("LIVE_EXECUTION_BLOCKED")
    audit_value = values.get(AUDIT_DB_PATH_ENV)
    audit_path = Path(audit_value).expanduser() if audit_value else None
    if audit_path is None:
        reasons.append("AUDIT_PERSISTENCE_NOT_CONFIGURED")
    timeout: float | None = None
    timeout_value = values.get(OPERATION_TIMEOUT_SECONDS_ENV)
    if timeout_value is not None:
        try:
            timeout = float(timeout_value)
        except ValueError:
            reasons.append("INVALID_OPERATION_TIMEOUT")
        else:
            if not 0 < timeout <= 30:
                reasons.append("INVALID_OPERATION_TIMEOUT")
    else:
        timeout = 5.0
    return ConfigurationValidation(
        valid=not reasons,
        execution_mode=mode,
        audit_path=audit_path,
        operation_timeout_seconds=timeout,
        reason_codes=tuple(sorted(set(reasons))),
    )
