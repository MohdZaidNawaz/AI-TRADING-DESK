"""Bounded, thread-safe passive health state; callers publish observed results."""

from __future__ import annotations

from threading import RLock

from app.health.models import ComponentHealth


class HealthStore:
    def __init__(self, *, max_components: int = 64) -> None:
        if max_components < 1:
            raise ValueError("max_components must be positive")
        self._max_components = max_components
        self._items: dict[str, ComponentHealth] = {}
        self._lock = RLock()

    def report(self, health: ComponentHealth) -> ComponentHealth:
        with self._lock:
            if health.component not in self._items and len(self._items) >= self._max_components:
                raise RuntimeError("HEALTH_STORE_CAPACITY")
            previous = self._items.get(health.component)
            if previous is not None and health.checked_at < previous.checked_at:
                return previous
            self._items[health.component] = health
            return health

    def get(self, component: str) -> ComponentHealth | None:
        with self._lock:
            return self._items.get(component)

    def snapshot(self) -> tuple[ComponentHealth, ...]:
        with self._lock:
            return tuple(self._items[key] for key in sorted(self._items))
