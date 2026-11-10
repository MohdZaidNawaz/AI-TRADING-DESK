from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CoordinationModel(BaseModel):
    """Immutable, additive API models for one non-executing analysis run."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentExecutionState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class AgentOutputKind(str, Enum):
    VERIFIED_OBSERVATION = "verified_observation"
    DETERMINISTIC_CALCULATION = "deterministic_calculation"
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"
    UNAVAILABLE = "unavailable"


class SpecialistRole(str, Enum):
    TECHNICAL = "technical"
    MACRO = "macro"
    NEWS_SESSION = "news_session"
    LIQUIDITY = "liquidity"
    STRATEGY = "strategy"
    RISK = "risk"
    EXECUTION_CONTROL = "execution_control"
    JOURNAL = "journal"
    QUANTITATIVE = "quantitative"


class CoordinationConsensus(str, Enum):
    AGREEMENT = "agreement"
    CONFLICTED = "conflicted"
    INSUFFICIENT_DATA = "insufficient_data"


class AgentEvidenceLink(CoordinationModel):
    event_id: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=80)
    domain: str = Field(min_length=1, max_length=60)
    observed_at: datetime
    retrieved_at: datetime | None = None
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    symbol: str | None = Field(default=None, max_length=40)

    @field_validator("observed_at", "retrieved_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coordination timestamps must include a timezone")
        return value.astimezone(UTC)


class AgentTaskPlan(CoordinationModel):
    task_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=80)
    role: SpecialistRole
    selected: bool
    dependency_agent_ids: tuple[str, ...] = ()


class AgentExecutionTransition(CoordinationModel):
    state: AgentExecutionState
    at: datetime

    @field_validator("at")
    @classmethod
    def normalize_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("transition timestamps must include a timezone")
        return value.astimezone(UTC)


class AgentExecutionRecord(CoordinationModel):
    task_id: str
    agent_id: str
    state: AgentExecutionState
    transitions: tuple[AgentExecutionTransition, ...]
    started_at: datetime | None = None
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    dependency_agent_ids: tuple[str, ...] = ()
    error_category: Literal["agent_error", "timeout", "cancelled", "dependency_unavailable"] | None = None
    evidence_reference_count: int = Field(ge=0)
    missing_inputs: tuple[str, ...] = ()
    output_kinds: tuple[AgentOutputKind, ...] = ()

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_execution_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_execution(self) -> "AgentExecutionRecord":
        if not self.transitions or self.transitions[-1].state is not self.state:
            raise ValueError("the final transition must match the execution state")
        if self.started_at is not None and self.completed_at < self.started_at:
            raise ValueError("execution completion cannot precede its start")
        return self


class AgentCoordinationReport(CoordinationModel):
    correlation_id: str = Field(min_length=1, max_length=160)
    planned_at: datetime
    completed_at: datetime
    tasks: tuple[AgentTaskPlan, ...]
    executions: tuple[AgentExecutionRecord, ...]
    consensus: CoordinationConsensus
    agreement_agent_ids: tuple[str, ...] = ()
    disagreement_agent_ids: tuple[str, ...] = ()
    missing_agent_ids: tuple[str, ...] = ()
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("planned_at", "completed_at")
    @classmethod
    def normalize_report_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("report timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_report(self) -> "AgentCoordinationReport":
        if self.completed_at < self.planned_at:
            raise ValueError("coordination completion cannot precede planning")
        task_ids = tuple(item.task_id for item in self.tasks)
        execution_ids = tuple(item.task_id for item in self.executions)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("coordination tasks must be unique")
        if task_ids != execution_ids:
            raise ValueError("execution records must preserve deterministic task order")
        return self
