"""Pure deterministic replay ordering. Consumers choose how to inspect references."""

from __future__ import annotations

from hashlib import sha256
import json

from app.replay.models import ReplayResult, ReplayScenario


class DeterministicReplay:
    def replay(self, scenario: ReplayScenario) -> ReplayResult:
        event_ids = tuple(event.event_id for event in scenario.ordered_events)
        fingerprint = sha256(json.dumps({"scenario": scenario.fingerprint, "events": event_ids}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ReplayResult(scenario_id=scenario.scenario_id, scenario_fingerprint=scenario.fingerprint, replayed_event_ids=event_ids, result_fingerprint=fingerprint)
