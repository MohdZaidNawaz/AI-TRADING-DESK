"""Typed contracts for immutable, research-only historical datasets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.market_series import NormalizedMarketBar, canonical_timeframe


class HistoricalDatasetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class DatasetIngestionStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    STORAGE_ERROR = "storage_error"


class HistoricalDatasetIngestionRequest(HistoricalDatasetModel):
    symbol: str = Field(min_length=1, max_length=120)
    timeframe: str = Field(min_length=2, max_length=20)
    start_at: datetime
    end_at: datetime
    max_requests: int = Field(default=3, ge=1, le=10)
    max_bars: int = Field(default=15_000, ge=1, le=50_000)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_timeframe(cls, value: object) -> object:
        return canonical_timeframe(value) if isinstance(value, str) else value

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dataset timestamps require a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_bounds(self) -> "HistoricalDatasetIngestionRequest":
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be earlier than end_at")
        if self.end_at - self.start_at > timedelta(days=3_653):
            raise ValueError("historical ingestion range cannot exceed ten years")
        return self


class HistoricalDatasetMetadata(HistoricalDatasetModel):
    schema_version: Literal[1] = 1
    dataset_version: Literal[1] = 1
    dataset_id: str = Field(min_length=20, max_length=96)
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: Literal["twelve_data"] = "twelve_data"
    provider_symbol: str = Field(min_length=1, max_length=120)
    canonical_symbol: str = Field(min_length=1, max_length=120)
    canonical_instrument_id: str | None = Field(default=None, max_length=160)
    requested_provider_exchange_id: str | None = Field(default=None, max_length=120)
    timeframe: str = Field(min_length=2, max_length=20)
    venue: str | None = Field(default=None, max_length=120)
    exchange_metadata_available: bool
    provider_identity_verified: bool
    adjustment_semantics: Literal["provider_unspecified"] = "provider_unspecified"
    requested_start_at: datetime
    requested_end_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    retrieved_at: datetime
    validated_bar_count: int = Field(ge=1, le=50_000)
    completed_bar_count: int = Field(ge=0, le=50_000)
    provisional_bar_count: int = Field(ge=0, le=50_000)
    observed_gap_count: int = Field(ge=0)
    missing_interval_count: None = None
    provider_request_count: int = Field(ge=1, le=10)
    completeness: DatasetCompleteness
    warning_codes: tuple[str, ...] = ()
    created_at: datetime

    @field_validator(
        "requested_start_at", "requested_end_at", "coverage_start", "coverage_end",
        "retrieved_at", "created_at",
    )
    @classmethod
    def normalize_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dataset metadata timestamps require a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_metadata(self) -> "HistoricalDatasetMetadata":
        if self.requested_start_at >= self.requested_end_at:
            raise ValueError("dataset requested range is invalid")
        if self.coverage_start >= self.coverage_end:
            raise ValueError("dataset coverage is invalid")
        if self.completed_bar_count + self.provisional_bar_count != self.validated_bar_count:
            raise ValueError("dataset candle counts are inconsistent")
        return self


class HistoricalDatasetRecord(HistoricalDatasetModel):
    metadata: HistoricalDatasetMetadata
    bars: tuple[NormalizedMarketBar, ...] = Field(min_length=1, max_length=50_000)


class HistoricalDatasetIngestionResponse(HistoricalDatasetModel):
    status: DatasetIngestionStatus
    dataset: HistoricalDatasetMetadata | None = None
    provider_request_count: int = Field(ge=0, le=10)
    received_bar_count: int = Field(ge=0)
    validated_bar_count: int = Field(ge=0)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    error_category: str | None = Field(default=None, max_length=80)
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False


class HistoricalDatasetListResponse(HistoricalDatasetModel):
    datasets: tuple[HistoricalDatasetMetadata, ...] = ()
    corrupt_dataset_count: int = Field(default=0, ge=0)
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False


class HistoricalDatasetDetailResponse(HistoricalDatasetModel):
    dataset: HistoricalDatasetMetadata
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False
