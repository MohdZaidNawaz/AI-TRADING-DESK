from pathlib import Path

import pytest

from app.execution.pilot import PilotExecutionCoordinator
from tests.test_execution import approved_execution_pair


def test_valid_sentinel_approval_creates_intent_without_consuming_approval() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    pilot = PilotExecutionCoordinator(registry)
    intent, result = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)
    assert intent is not None
    assert result.status.value == "adapter_unavailable"
    assert result.adapter_status == "ADAPTER_NOT_CONNECTED"
    # ``verify`` remains available after preparation: only a future adapter
    # submission boundary may use the consuming ``claim`` operation.
    verified = registry.verify(proposal=proposal, decision=decision, now=now)
    assert verified.approval_id == decision.approval_id


def test_repeated_valid_preparation_is_safe_and_deterministic() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    pilot = PilotExecutionCoordinator(registry)
    first, _ = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)
    second, _ = pilot.prepare_execution(proposal=proposal, decision=decision, now=now)
    assert first is not None and second is not None
    assert first.execution_intent_id == second.execution_intent_id


@pytest.mark.parametrize(
    "change",
    [
        {"approval_id": "unknown"},
        {"policy_id": "other-policy"},
        {"assessment_id": "other-assessment"},
        {"account_id": "other-account"},
    ],
)
def test_pilot_rejects_unregistered_or_tampered_approvals(change: dict[str, object]) -> None:
    proposal, decision, registry, now = approved_execution_pair()
    intent, result = PilotExecutionCoordinator(registry).prepare_execution(
        proposal=proposal, decision=decision.model_copy(update=change), now=now
    )
    assert intent is None
    assert result.status.value == "not_executed"


def test_pilot_rejects_consumed_approval() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    registry.claim(proposal=proposal, decision=decision, now=now)
    intent, result = PilotExecutionCoordinator(registry).prepare_execution(
        proposal=proposal, decision=decision, now=now
    )
    assert intent is None
    assert result.status.value == "not_executed"


def test_pilot_cannot_bypass_sentinel_or_modify_parameters() -> None:
    proposal, decision, registry, now = approved_execution_pair()
    modified = proposal.model_copy(
        update={"parameters": proposal.parameters.model_copy(update={"reference_price": 101.0})}
    )
    intent, result = PilotExecutionCoordinator(registry).prepare_execution(
        proposal=modified, decision=decision, now=now
    )
    assert intent is None
    assert "APPROVAL_FINGERPRINT_MISMATCH" in result.reason_codes


def test_phase_eight_contains_no_mt5_broker_credentials_or_network_execution() -> None:
    root = Path(__file__).resolve().parents[1]
    text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (
            root / "app/models/execution.py",
            root / "app/execution/adapter.py",
            root / "app/execution/intent_store.py",
            root / "app/execution/pilot.py",
            root / "app/services/execution_validation.py",
        )
    )
    forbidden = ("metatrader5", "order_send", "requests.", "httpx.", "broker.connect", "password", "api_key")
    assert not any(value in text for value in forbidden)
