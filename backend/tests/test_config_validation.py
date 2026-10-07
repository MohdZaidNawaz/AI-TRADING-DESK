from app.services.config_validation import validate_runtime_configuration


def test_runtime_configuration_defaults_fail_closed_without_audit_path() -> None:
    result = validate_runtime_configuration({})
    assert not result.valid
    assert "AUDIT_PERSISTENCE_NOT_CONFIGURED" in result.reason_codes
    assert result.execution_mode == "disabled"


def test_runtime_configuration_rejects_live_and_malformed_values() -> None:
    result = validate_runtime_configuration({"MT5_EXECUTION_MODE": "live", "FRIDAY_AUDIT_DB_PATH": "audit.sqlite3", "FRIDAY_OPERATION_TIMEOUT_SECONDS": "forever"})
    assert not result.valid
    assert "LIVE_EXECUTION_BLOCKED" in result.reason_codes
    assert "INVALID_OPERATION_TIMEOUT" in result.reason_codes


def test_runtime_configuration_accepts_dry_run_without_reading_credentials() -> None:
    result = validate_runtime_configuration({"MT5_EXECUTION_MODE": "dry_run", "FRIDAY_AUDIT_DB_PATH": "audit.sqlite3", "FRIDAY_OPERATION_TIMEOUT_SECONDS": "5"})
    assert result.valid
    assert result.execution_mode == "dry_run"
