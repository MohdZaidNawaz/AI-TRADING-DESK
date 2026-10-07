from app import config
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


def test_runtime_settings_use_process_values_before_local_env_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "dotenv_values",
        lambda _path: {
            "FRIDAY_AUDIT_DB_PATH": "local-audit.sqlite3",
            "FRIDAY_OPERATION_TIMEOUT_SECONDS": "4",
            "MT5_EXECUTION_MODE": "disabled",
        },
    )
    monkeypatch.setenv("FRIDAY_AUDIT_DB_PATH", "process-audit.sqlite3")
    values = config.get_runtime_settings()
    assert values["FRIDAY_AUDIT_DB_PATH"] == "process-audit.sqlite3"
    assert values["FRIDAY_OPERATION_TIMEOUT_SECONDS"] == "4"
    assert values["MT5_EXECUTION_MODE"] == "disabled"
