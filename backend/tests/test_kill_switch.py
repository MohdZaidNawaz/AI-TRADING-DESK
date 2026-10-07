import pytest

from app.execution.kill_switch import ExecutionKillSwitch


def test_kill_switch_is_deterministic_and_requires_actor_reason() -> None:
    switch = ExecutionKillSwitch()
    assert not switch.status().active
    assert switch.trigger(reason="operator stop", changed_by="operator-1").active
    assert switch.status().reason == "operator stop"
    assert not switch.reset(reason="review complete", changed_by="operator-2").active
    with pytest.raises(ValueError):
        switch.trigger(reason="", changed_by="operator")
    with pytest.raises(ValueError):
        switch.reset(reason="x", changed_by="")
