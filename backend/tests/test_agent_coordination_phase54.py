from __future__ import annotations

from hashlib import sha256
from threading import Event, Thread
from time import sleep

import pytest

from app.agents.base import AnalysisContext, BaseAgent
from app.agents.master import MasterAgent
from app.agents.orchestrator import FridayCore, create_friday_core
from app.models.agent_coordination import AgentExecutionState, AgentOutputKind
from app.models.schemas import AgentAnalysis, AnalyzeRequest


class ControlledAgent(BaseAgent):
    expertise = "controlled offline test specialist"

    def __init__(
        self,
        agent_id: str,
        *,
        started: Event | None = None,
        peer_started: Event | None = None,
        release: Event | None = None,
        fail: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.name = agent_id.replace("_", " ").title()
        self.started = started
        self.peer_started = peer_started
        self.release = release
        self.fail = fail

    def analyze(self, context: AnalysisContext) -> AgentAnalysis:
        if self.started is not None:
            self.started.set()
        overlap = self.peer_started.wait(0.5) if self.peer_started is not None else True
        if self.release is not None:
            self.release.wait(2.0)
        if self.fail:
            raise RuntimeError("controlled failure")
        return self.result(
            summary="Controlled deterministic specialist result.",
            findings=[
                "parallel_overlap=" + str(overlap).lower(),
                "dependencies="
                + ",".join(item.agent_id for item in context.lifecycle.prior_results),
            ],
            limitations=["Controlled fixture has no provider evidence."],
            confidence=0.2,
        )


def _core(*agents: BaseAgent, timeout: float = 1.0) -> FridayCore:
    return FridayCore(
        specialists=list(agents),
        master=MasterAgent(),
        agent_timeout_seconds=timeout,
    )


def test_independent_specialists_execute_in_parallel_and_results_remain_ordered() -> None:
    atlas_started = Event()
    scout_started = Event()
    core = _core(
        ControlledAgent(
            "atlas_macro",
            started=atlas_started,
            peer_started=scout_started,
        ),
        ControlledAgent(
            "scout_recon",
            started=scout_started,
            peer_started=atlas_started,
        ),
    )

    response = core.analyze(AnalyzeRequest(query="complete overview"))

    assert [item.agent_id for item in response.agent_results] == [
        "atlas_macro",
        "scout_recon",
    ]
    assert all("parallel_overlap=true" in item.findings for item in response.agent_results)
    assert response.coordination is not None
    assert [item.agent_id for item in response.coordination.executions] == [
        "atlas_macro",
        "scout_recon",
    ]


def test_strategy_dependency_receives_prior_result_metadata() -> None:
    core = _core(
        ControlledAgent("atlas_macro"),
        ControlledAgent("athena_analyst"),
    )

    response = core.analyze(AnalyzeRequest(query="complete overview"))

    athena = next(item for item in response.agent_results if item.agent_id == "athena_analyst")
    task = next(item for item in response.coordination.tasks if item.agent_id == "athena_analyst")
    assert task.dependency_agent_ids == ("atlas_macro",)
    assert "dependencies=atlas_macro" in athena.findings
    assert athena.execution_state is AgentExecutionState.PARTIAL


def test_failed_specialist_is_explicit_and_does_not_abort_other_specialists() -> None:
    core = _core(
        ControlledAgent("atlas_macro", fail=True),
        ControlledAgent("scout_recon"),
    )

    response = core.analyze(AnalyzeRequest(query="complete overview"))

    atlas = next(item for item in response.agent_results if item.agent_id == "atlas_macro")
    scout = next(item for item in response.agent_results if item.agent_id == "scout_recon")
    atlas_execution = next(
        item for item in response.coordination.executions if item.agent_id == "atlas_macro"
    )
    assert atlas.status == "error"
    assert atlas.execution_state is AgentExecutionState.FAILED
    assert atlas_execution.error_category == "agent_error"
    assert scout.status == "completed"


def test_timeout_discards_late_agent_output_without_retry() -> None:
    release = Event()
    core = _core(ControlledAgent("atlas_macro", release=release), timeout=0.02)

    response = core.analyze(AnalyzeRequest(query="complete overview"))
    release.set()

    result = response.agent_results[0]
    execution = response.coordination.executions[0]
    assert result.status == "error"
    assert result.execution_state is AgentExecutionState.FAILED
    assert execution.error_category == "timeout"
    assert execution.transitions[-1].state is AgentExecutionState.FAILED


def test_active_analysis_can_be_cancelled_and_late_output_is_discarded() -> None:
    started = Event()
    release = Event()
    query = "complete overview"
    analysis_id = "mock-" + sha256(f"\n{query}".encode()).hexdigest()[:16]
    core = _core(
        ControlledAgent("atlas_macro", started=started, release=release),
        timeout=0.05,
    )
    captured: list[object] = []

    worker = Thread(
        target=lambda: captured.append(core.analyze(AnalyzeRequest(query=query))),
        daemon=True,
    )
    worker.start()
    assert started.wait(1.0)
    assert core.cancel_analysis(analysis_id) is True
    worker.join(1.0)
    release.set()

    assert len(captured) == 1
    response = captured[0]
    assert response.coordination.executions[0].error_category == "cancelled"
    assert response.agent_results[0].execution_state is AgentExecutionState.FAILED


def test_duplicate_specialist_ids_are_rejected_before_execution() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        _core(ControlledAgent("atlas_macro"), ControlledAgent("atlas_macro"))


def test_default_coordination_is_fail_closed_and_non_authorizing() -> None:
    response = create_friday_core().analyze(
        AnalyzeRequest(symbol="XAUUSD", query="Give me a complete overview")
    )

    assert response.coordination is not None
    assert response.coordination.production_authorization_available is False
    assert response.coordination.submission_allowed is False
    assert response.coordination.execution_authority is False
    assert response.coordination.missing_agent_ids
    pilot = next(item for item in response.agent_results if item.agent_id == "pilot_execution")
    assert pilot.status == "completed"  # legacy compatibility field
    assert pilot.execution_state is AgentExecutionState.UNAVAILABLE
    assert pilot.output_kinds == [AgentOutputKind.UNAVAILABLE]
    assert "execution_capability_disabled" in pilot.missing_inputs


def test_runtime_telemetry_is_additive_and_legacy_fields_remain_available() -> None:
    response = create_friday_core().analyze(
        AnalyzeRequest(symbol="EURUSD", query="Review the latest news catalysts")
    )
    payload = response.model_dump(mode="json")

    assert payload["mode"] == "deterministic_mock"
    assert payload["routing"]["selected_agents"] == payload["selected_agents"]
    assert all("status" in item and "summary" in item for item in payload["agent_results"])
    assert payload["coordination"]["correlation_id"] == payload["analysis_id"]
    assert payload["synthesis"]["missing_agent_ids"]
