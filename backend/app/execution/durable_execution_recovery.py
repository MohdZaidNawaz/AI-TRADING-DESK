"""Idempotent, non-submitting recovery for Phase 13 durable reservations."""

from __future__ import annotations

from datetime import datetime

from app.execution.durable_reservation_store import DurableReservationStore
from app.models.durable_execution import DurableExecutionReservation


class DurableExecutionRecoveryService:
    """Require reconciliation after restart; never inspect or call a broker."""

    def __init__(self, store: DurableReservationStore) -> None:
        self._store = store

    def recover(self, *, now: datetime) -> tuple[DurableExecutionReservation, ...]:
        return self._store.recover(now=now)
