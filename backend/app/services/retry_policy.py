"""Deterministic retry classification; it never retries execution submission."""

from __future__ import annotations

from enum import Enum


class FailureCategory(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class RetryPolicy:
    """Pure retry plan. Scheduling is left to a caller; no sleep or network work occurs."""

    def __init__(self, *, max_attempts: int = 3, base_delay_seconds: float = 0.25, max_delay_seconds: float = 2.0) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not 0 < base_delay_seconds <= max_delay_seconds <= 30:
            raise ValueError("retry delays must be bounded and positive")
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds

    def retry_after_seconds(self, *, category: FailureCategory, attempt: int, operation: str, provider_retry_after_seconds: float | None = None) -> float | None:
        if attempt < 1:
            raise ValueError("attempt must be positive")
        if operation in {"execution_submission", "broker_submission", "reconciliation"}:
            return None
        if category not in {FailureCategory.TRANSIENT, FailureCategory.TIMEOUT, FailureCategory.UNAVAILABLE, FailureCategory.RATE_LIMITED}:
            return None
        if attempt >= self._max_attempts:
            return None
        if category is FailureCategory.RATE_LIMITED and provider_retry_after_seconds is not None:
            if not 0 < provider_retry_after_seconds <= 30:
                return None
            return provider_retry_after_seconds
        return min(self._base_delay_seconds * (2 ** (attempt - 1)), self._max_delay_seconds)


def classify_http_failure(status_code: int | None, *, timeout: bool = False, authentication: bool = False) -> FailureCategory:
    if timeout:
        return FailureCategory.TIMEOUT
    if authentication or status_code in {401, 403}:
        return FailureCategory.AUTHENTICATION
    if status_code == 429:
        return FailureCategory.RATE_LIMITED
    if status_code is not None and 500 <= status_code <= 599:
        return FailureCategory.UNAVAILABLE
    if status_code is not None and 400 <= status_code <= 499:
        return FailureCategory.PERMANENT
    return FailureCategory.UNKNOWN
