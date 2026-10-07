from datetime import UTC, datetime, timedelta

import pytest

from app.execution.mt5_adapter import MT5ExecutionAdapter, MT5Settings
from app.health.models import ComponentHealth, HealthStatus
from app.health.service import HealthService
from app.health.store import HealthStore


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_health_store_is_bounded_thread_safe_and_preserves_newest_observation() -> None:
    store = HealthStore(max_components=1)
    service = HealthService(store)
    healthy = service.observed(component="alpha_vantage", status=HealthStatus.HEALTHY, checked_at=NOW, latency_ms=12.0)
    stale = service.observed(component="alpha_vantage", status=HealthStatus.UNAVAILABLE, checked_at=NOW - timedelta(seconds=1), reason_code="old")
    assert stale == healthy
    assert store.snapshot() == (healthy,)
    with pytest.raises(RuntimeError):
        service.observed(component="twelve_data", status=HealthStatus.UNKNOWN, checked_at=NOW)


def test_mt5_health_is_passive_and_unavailable_without_configuration() -> None:
    adapter = MT5ExecutionAdapter(settings=MT5Settings(login=None))
    before = adapter.health_snapshot(checked_at=NOW)
    assert before.status is HealthStatus.UNKNOWN
    assert adapter.availability() == "MT5_NOT_CONFIGURED"
    after = adapter.health_snapshot(checked_at=NOW)
    assert after.status is HealthStatus.UNAVAILABLE
