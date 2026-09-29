"""Narrow environment-backed configuration helpers for FRIDAY."""

import os
from pathlib import Path

from dotenv import dotenv_values

TWELVE_DATA_API_KEY_ENV = "TWELVE_DATA_API_KEY"
ALPHA_VANTAGE_API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"
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


def _non_blank(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
