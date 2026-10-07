from datetime import UTC, datetime

from app.replay.models import ReplayEvent, ReplayScenario
from app.replay.service import DeterministicReplay


def _event(domain: str, reference: str) -> ReplayEvent:
    return ReplayEvent(domain=domain, reference_id=reference, fingerprint=(domain[0] * 64), occurred_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_replay_is_deterministic_without_network_or_provider_access() -> None:
    first = ReplayScenario(scenario_id="case-1", events=(_event("market", "b"), _event("news", "a")))
    second = ReplayScenario(scenario_id="case-1", events=tuple(reversed(first.events)))
    replay = DeterministicReplay()
    assert replay.replay(first) == replay.replay(second)
