"""Narrow environment-backed configuration helpers for FRIDAY."""

import os
from pathlib import Path

from dotenv import dotenv_values

TWELVE_DATA_API_KEY_ENV = "TWELVE_DATA_API_KEY"
ALPHA_VANTAGE_API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"
FRED_API_KEY_ENV = "FRED_API_KEY"
BLS_API_KEY_ENV = "BLS_API_KEY"
BEA_API_USER_ID_ENV = "BEA_API_USER_ID"
TRADING_ECONOMICS_API_KEY_ENV = "TRADING_ECONOMICS_API_KEY"
TRADING_ECONOMICS_COMMERCIAL_AUTHORIZED_ENV = "TRADING_ECONOMICS_COMMERCIAL_AUTHORIZED"
TRADING_ECONOMICS_REDISTRIBUTION_AUTHORIZED_ENV = (
    "TRADING_ECONOMICS_REDISTRIBUTION_AUTHORIZED"
)
MACRO_ENABLED_PROVIDERS_ENV = "FRIDAY_MACRO_ENABLED_PROVIDERS"
MT5_LOGIN_ENV = "MT5_LOGIN"
MT5_PASSWORD_ENV = "MT5_PASSWORD"
MT5_SERVER_ENV = "MT5_SERVER"
MT5_PATH_ENV = "MT5_PATH"
MT5_EXECUTION_MODE_ENV = "MT5_EXECUTION_MODE"
AUDIT_DB_PATH_ENV = "FRIDAY_AUDIT_DB_PATH"
OPERATION_TIMEOUT_SECONDS_ENV = "FRIDAY_OPERATION_TIMEOUT_SECONDS"
SHARED_AUTHORITY_DB_PATH_ENV = "FRIDAY_SHARED_AUTHORITY_DB_PATH"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_MODEL_ENV = "OPENAI_MODEL"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV = "ANTHROPIC_MODEL"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_MODEL_ENV = "GEMINI_MODEL"
LLM_PROVIDER_MODE_ENV = "FRIDAY_LLM_PROVIDER_MODE"
LLM_DEFAULT_PROVIDER_ENV = "FRIDAY_LLM_DEFAULT_PROVIDER"
LLM_MANUAL_FALLBACK_ENV = "FRIDAY_LLM_MANUAL_FALLBACK_ENABLED"
LLM_TIMEOUT_SECONDS_ENV = "FRIDAY_LLM_TIMEOUT_SECONDS"
LLM_MAX_ATTEMPTS_ENV = "FRIDAY_LLM_MAX_ATTEMPTS"
LLM_RETRY_BASE_SECONDS_ENV = "FRIDAY_LLM_RETRY_BASE_SECONDS"
LLM_MAX_RETRY_DELAY_SECONDS_ENV = "FRIDAY_LLM_MAX_RETRY_DELAY_SECONDS"
LLM_MAX_OUTPUT_TOKENS_ENV = "FRIDAY_LLM_MAX_OUTPUT_TOKENS"
LLM_BUDGET_DB_PATH_ENV = "FRIDAY_LLM_BUDGET_DB_PATH"
LLM_MONTHLY_BUDGET_INR_ENV = "FRIDAY_LLM_MONTHLY_BUDGET_INR"
LLM_USABLE_BUDGET_INR_ENV = "FRIDAY_LLM_USABLE_BUDGET_INR"
LLM_PROTECTED_RESERVE_INR_ENV = "FRIDAY_LLM_PROTECTED_RESERVE_INR"
LLM_DAILY_LIMIT_INR_ENV = "FRIDAY_LLM_DAILY_LIMIT_INR"
LLM_MAX_REQUEST_ESTIMATE_INR_ENV = "FRIDAY_LLM_MAX_REQUEST_ESTIMATE_INR"
LLM_PAID_PROVIDER_AUTHORIZED_ENV = "FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED"
LLM_PROVIDER_PRICING_JSON_ENV = "FRIDAY_LLM_PROVIDER_PRICING_JSON"
LLM_INR_PER_USD_ENV = "FRIDAY_LLM_INR_PER_USD"
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


def get_fred_api_key(*, env_file: Path | None = None) -> str | None:
    return _get_optional_secret(FRED_API_KEY_ENV, env_file=env_file)


def get_bls_api_key(*, env_file: Path | None = None) -> str | None:
    return _get_optional_secret(BLS_API_KEY_ENV, env_file=env_file)


def get_bea_api_user_id(*, env_file: Path | None = None) -> str | None:
    return _get_optional_secret(BEA_API_USER_ID_ENV, env_file=env_file)


def get_trading_economics_api_key(*, env_file: Path | None = None) -> str | None:
    return _get_optional_secret(TRADING_ECONOMICS_API_KEY_ENV, env_file=env_file)


def get_macro_enabled_providers(*, env_file: Path | None = None) -> frozenset[str]:
    raw = _get_optional_secret(MACRO_ENABLED_PROVIDERS_ENV, env_file=env_file)
    if raw is None:
        return frozenset()
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def get_trading_economics_authorizations(
    *, env_file: Path | None = None
) -> tuple[bool, bool]:
    commercial = _get_optional_secret(
        TRADING_ECONOMICS_COMMERCIAL_AUTHORIZED_ENV,
        env_file=env_file,
    )
    redistribution = _get_optional_secret(
        TRADING_ECONOMICS_REDISTRIBUTION_AUTHORIZED_ENV,
        env_file=env_file,
    )
    return _strict_enabled(commercial), _strict_enabled(redistribution)


def _get_optional_secret(name: str, *, env_file: Path | None = None) -> str | None:
    process_value = _non_blank(os.getenv(name))
    if process_value is not None:
        return process_value
    file_path = env_file or BACKEND_ENV_FILE
    if not file_path.is_file():
        return None
    return _non_blank(dotenv_values(file_path).get(name))


def _strict_enabled(value: str | None) -> bool:
    return value is not None and value.strip().casefold() == "true"


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


def get_llm_environment() -> dict[str, str | None]:
    """Read only process-level LLM settings; keys never leave the backend."""
    names = (
        OPENAI_API_KEY_ENV,
        OPENAI_MODEL_ENV,
        ANTHROPIC_API_KEY_ENV,
        ANTHROPIC_MODEL_ENV,
        GEMINI_API_KEY_ENV,
        GEMINI_MODEL_ENV,
        LLM_PROVIDER_MODE_ENV,
        LLM_DEFAULT_PROVIDER_ENV,
        LLM_MANUAL_FALLBACK_ENV,
        LLM_TIMEOUT_SECONDS_ENV,
        LLM_MAX_ATTEMPTS_ENV,
        LLM_RETRY_BASE_SECONDS_ENV,
        LLM_MAX_RETRY_DELAY_SECONDS_ENV,
        LLM_MAX_OUTPUT_TOKENS_ENV,
        LLM_BUDGET_DB_PATH_ENV,
        LLM_MONTHLY_BUDGET_INR_ENV,
        LLM_USABLE_BUDGET_INR_ENV,
        LLM_PROTECTED_RESERVE_INR_ENV,
        LLM_DAILY_LIMIT_INR_ENV,
        LLM_MAX_REQUEST_ESTIMATE_INR_ENV,
        LLM_PAID_PROVIDER_AUTHORIZED_ENV,
        LLM_PROVIDER_PRICING_JSON_ENV,
        LLM_INR_PER_USD_ENV,
    )
    return {name: _non_blank(os.getenv(name)) for name in names}


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


def get_shared_authority_db_path() -> Path:
    """Return the non-secret durable approval database path.

    A process value may select an explicit local path.  The safe default lives
    under the already ignored backend runtime directory; failure to initialize
    this database is handled as an approval fail-closed condition.
    """
    configured = _non_blank(os.getenv(SHARED_AUTHORITY_DB_PATH_ENV))
    if configured is not None:
        return Path(configured)
    return BACKEND_ENV_FILE.parent / "runtime" / "shared_authority.sqlite3"
