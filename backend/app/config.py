"""Narrow environment-backed configuration helpers for FRIDAY."""

import os
from pathlib import Path

from dotenv import dotenv_values

TWELVE_DATA_API_KEY_ENV = "TWELVE_DATA_API_KEY"
ALPHA_VANTAGE_API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"
MT5_LOGIN_ENV = "MT5_LOGIN"
MT5_PASSWORD_ENV = "MT5_PASSWORD"
MT5_SERVER_ENV = "MT5_SERVER"
MT5_PATH_ENV = "MT5_PATH"
MT5_EXECUTION_MODE_ENV = "MT5_EXECUTION_MODE"
AUDIT_DB_PATH_ENV = "FRIDAY_AUDIT_DB_PATH"
OPERATION_TIMEOUT_SECONDS_ENV = "FRIDAY_OPERATION_TIMEOUT_SECONDS"
BACKEND_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def get_twelve_data_api_key(
    *,
    env_file: Path | None = None,
    load_env_file: bool = True,
) -> str | None:
    """Return the process value, or a Twelve Data-only local .env fallback."""
    process_value = os.getenv(TWELVE_DATA_API_KEY_ENV)
    if process_value is not None:
        return _non_blank(process_value)

    if not load_env_file:
        return None

    file_path = env_file or BACKEND_ENV_FILE
    try:
        file_value = dotenv_values(file_path).get(TWELVE_DATA_API_KEY_ENV)
    except OSError:
        return None
    return _non_blank(file_value)


def get_alpha_vantage_api_key(
    *,
    env_file: Path | None = None,
    load_env_file: bool = True,
) -> str | None:
    """Return the process value, or an Alpha Vantage-only local .env fallback."""
    process_value = os.getenv(ALPHA_VANTAGE_API_KEY_ENV)
    if process_value is not None:
        return _non_blank(process_value)

    if not load_env_file:
        return None

    file_path = env_file or BACKEND_ENV_FILE
    try:
        file_value = dotenv_values(file_path).get(ALPHA_VANTAGE_API_KEY_ENV)
    except OSError:
        return None
    return _non_blank(file_value)


def get_mt5_settings(
    *,
    env_file: Path | None = None,
    load_env_file: bool = True,
) -> dict[str, str | None]:
    """Read local MT5 settings without logging or otherwise exposing them.

    Process values take precedence over a local ``backend/.env`` fallback, the
    same convention used by the existing provider connectors.  Consumers must
    not serialize this dictionary because it can contain ``MT5_PASSWORD``.
    """
    names = (
        MT5_LOGIN_ENV,
        MT5_PASSWORD_ENV,
        MT5_SERVER_ENV,
        MT5_PATH_ENV,
        MT5_EXECUTION_MODE_ENV,
    )
    values: dict[str, str | None] = {name: _non_blank(os.getenv(name)) for name in names}
    if not load_env_file or all(value is not None for value in values.values()):
        return values

    try:
        file_values = dotenv_values(env_file or BACKEND_ENV_FILE)
    except OSError:
        return values
    for name in names:
        if values[name] is None:
            values[name] = _non_blank(file_values.get(name))
    return values


def _non_blank(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def get_runtime_settings(
    *,
    env_file: Path | None = None,
    load_env_file: bool = True,
) -> dict[str, str | None]:
    """Return non-secret operational settings using the established local fallback.

    Process settings retain precedence.  The local backend ``.env`` is a
    development-only fallback, matching the MT5 and provider configuration
    convention without ever serializing secrets.
    """
    names = (AUDIT_DB_PATH_ENV, OPERATION_TIMEOUT_SECONDS_ENV, MT5_EXECUTION_MODE_ENV)
    values = {name: _non_blank(os.getenv(name)) for name in names}
    if not load_env_file or all(value is not None for value in values.values()):
        return values
    try:
        file_values = dotenv_values(env_file or BACKEND_ENV_FILE)
    except OSError:
        return values
    for name in names:
        if values[name] is None:
            values[name] = _non_blank(file_values.get(name))
    return values
