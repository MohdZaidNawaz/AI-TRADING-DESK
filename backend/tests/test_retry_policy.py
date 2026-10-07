from app.services.retry_policy import FailureCategory, RetryPolicy, classify_http_failure


def test_retry_policy_retries_only_bounded_transient_provider_failures() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.25, max_delay_seconds=1.0)
    assert policy.retry_after_seconds(category=FailureCategory.TRANSIENT, attempt=1, operation="provider_fetch") == 0.25
    assert policy.retry_after_seconds(category=FailureCategory.TIMEOUT, attempt=2, operation="provider_fetch") == 0.5
    assert policy.retry_after_seconds(category=FailureCategory.RATE_LIMITED, attempt=1, operation="provider_fetch", provider_retry_after_seconds=1.5) == 1.5
    assert policy.retry_after_seconds(category=FailureCategory.PERMANENT, attempt=1, operation="provider_fetch") is None
    assert policy.retry_after_seconds(category=FailureCategory.UNKNOWN, attempt=1, operation="execution_submission") is None


def test_http_failure_classification_is_deterministic() -> None:
    assert classify_http_failure(429) is FailureCategory.RATE_LIMITED
    assert classify_http_failure(503) is FailureCategory.UNAVAILABLE
    assert classify_http_failure(401) is FailureCategory.AUTHENTICATION
    assert classify_http_failure(None, timeout=True) is FailureCategory.TIMEOUT
