"""Process-local deterministic submission blocker; it never cancels broker state."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    active: bool
    reason: str | None = None
    changed_by: str | None = None


class ExecutionKillSwitch:
    def __init__(self) -> None:
        self._status = KillSwitchStatus(active=False)
        self._lock = RLock()

    def status(self) -> KillSwitchStatus:
        with self._lock:
            return self._status

    def trigger(self, *, reason: str, changed_by: str) -> KillSwitchStatus:
        if not reason.strip() or not changed_by.strip():
            raise ValueError("kill switch trigger requires reason and actor")
        with self._lock:
            self._status = KillSwitchStatus(active=True, reason=reason.strip(), changed_by=changed_by.strip())
            return self._status

    def reset(self, *, reason: str, changed_by: str) -> KillSwitchStatus:
        if not reason.strip() or not changed_by.strip():
            raise ValueError("kill switch reset requires reason and actor")
        with self._lock:
            self._status = KillSwitchStatus(active=False, reason=None, changed_by=changed_by.strip())
            return self._status
