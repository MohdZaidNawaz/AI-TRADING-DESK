"""Small adapters for publishing existing operation outcomes as health state."""

from __future__ import annotations

from datetime import datetime

from app.health.models import ComponentHealth, HealthStatus
from app.health.store import HealthStore


class HealthService:
    def __init__(self, store: HealthStore) -> None:
        self._store = store

    def observed(
        self,
        *,
        component: str,
        status: HealthStatus,
        checked_at: datetime,
        latency_ms: float | None = None,
        reason_code: str | None = None,
        failure_category: str | None = None,
    ) -> ComponentHealth:
        previous = self._store.get(component)
        return self._store.report(
            ComponentHealth(
                component=component,
                status=status,
                checked_at=checked_at,
                latency_ms=latency_ms,
                last_success_at=checked_at if status is HealthStatus.HEALTHY else (previous.last_success_at if previous else None),
                failure_category=failure_category,
                reason_code=reason_code,
            )
        )

    def snapshot(self) -> tuple[ComponentHealth, ...]:
        return self._store.snapshot()
