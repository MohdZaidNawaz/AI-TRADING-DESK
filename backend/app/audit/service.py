"""Narrow audit service used at explicit safety boundaries."""

from __future__ import annotations

from app.audit.models import AuditEvent
from app.audit.store import AuditStore


class AuditService:
    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def available(self) -> bool:
        return self._store.is_available()

    def record(self, event: AuditEvent) -> AuditEvent:
        return self._store.append(event)

    def events(self) -> tuple[AuditEvent, ...]:
        return self._store.events()
