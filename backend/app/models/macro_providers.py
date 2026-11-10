"""Typed boundaries shared by the Phase 5.2 macro provider adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.models.economic_events import NormalizedEconomicEvent
from app.models.macro_intelligence import MacroProviderDiagnostic, MacroSourceType
from app.models.macro_series import NormalizedMacroObservation
from app.models.schemas import APIModel


class MacroProviderModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MacroProviderId(str, Enum):
    FRED = "fred"
    US_TREASURY = "us_treasury"
    BLS = "bls"
    BEA = "bea"
    FEDERAL_RESERVE = "federal_reserve"
    TRADING_ECONOMICS = "trading_economics"


class MacroProviderOperation(str, Enum):
    REFRESH = "refresh"
    HISTORICAL_BOOTSTRAP = "historical_bootstrap"
    VINTAGE_SNAPSHOT = "vintage_snapshot"


class MacroProviderFailureCategory(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER_ERROR = "provider_error"


class MacroProviderLicense(str, Enum):
    PUBLIC_GOVERNMENT = "public_government"
    FRED_TERMS = "fred_terms"
    COMMERCIAL_RESTRICTED = "commercial_restricted"
    UNVERIFIED = "unverified"


class MacroMappingStatus(str, Enum):
    ENABLED = "enabled"
    BLOCKED_UNVERIFIED = "blocked_unverified"
    DISABLED_COMMERCIAL = "disabled_commercial"


class MacroProviderConfiguration(MacroProviderModel):
    provider_id: MacroProviderId
    enabled: bool = False
    credential_configured: bool = False
    commercial_authorization: bool = False
    redistribution_authorization: bool = False
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    request_budget: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_commercial_provider(self) -> "MacroProviderConfiguration":
        if self.provider_id is MacroProviderId.TRADING_ECONOMICS and self.enabled:
            if not (
                self.credential_configured
                and self.commercial_authorization
                and self.redistribution_authorization
            ):
                raise ValueError(
                    "Trading Economics requires credentials and explicit commercial and "
                    "redistribution authorization"
                )
        return self


class MacroProviderRequest(MacroProviderModel):
    provider_id: MacroProviderId
    operation: MacroProviderOperation
    indicator_ids: tuple[str, ...]
    start_date: date | None = None
    end_date: date | None = None
    vintage_date: date | None = None
    max_records: int = Field(default=500, ge=1, le=10_000)
    requested_at: datetime

    @field_validator("indicator_ids")
    @classmethod
    def canonicalize_indicators(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
        if not normalized:
            raise ValueError("macro provider requests require at least one indicator")
        return normalized

    @field_validator("requested_at")
    @classmethod
    def normalize_requested_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro provider request time requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self) -> "MacroProviderRequest":
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("macro provider end date cannot precede start date")
        if (
            self.operation is MacroProviderOperation.VINTAGE_SNAPSHOT
            and self.vintage_date is None
        ):
            raise ValueError("vintage snapshot requests require vintage_date")
        return self


class MacroProviderMapping(MacroProviderModel):
    provider_id: MacroProviderId
    indicator_id: str = Field(min_length=1, max_length=120)
    provider_series_id: str | None = Field(default=None, max_length=160)
    original_publisher_id: str = Field(min_length=1, max_length=80)
    original_publisher_name: str = Field(min_length=1, max_length=120)
    source_type: MacroSourceType
    license: MacroProviderLicense
    status: MacroMappingStatus
    supports_vintages: bool = False
    notes: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_mapping(self) -> "MacroProviderMapping":
        if self.status is MacroMappingStatus.ENABLED and self.provider_series_id is None:
            raise ValueError("enabled provider mappings require a verified provider series id")
        return self


class MacroProviderBatch(MacroProviderModel):
    provider_id: MacroProviderId
    operation: MacroProviderOperation
    observations: tuple[NormalizedMacroObservation, ...] = ()
    events: tuple[NormalizedEconomicEvent, ...] = ()
    diagnostic: MacroProviderDiagnostic
    retrieved_at: datetime | None = None
    cache_preserved: bool = False

    @field_validator("retrieved_at")
    @classmethod
    def normalize_retrieved_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro provider retrieval time requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_counts(self) -> "MacroProviderBatch":
        validated = len(self.observations) + len(self.events)
        if self.diagnostic.validated_count != validated:
            raise ValueError("provider diagnostic validated count must match the batch")
        if validated and self.retrieved_at is None:
            raise ValueError("successful provider batches require retrieved_at")
        return self


class MacroRefreshOutcome(MacroProviderModel):
    provider_id: MacroProviderId
    operation: MacroProviderOperation
    diagnostic: MacroProviderDiagnostic
    ingested_observation_ids: tuple[str, ...] = ()
    ingested_event_ids: tuple[str, ...] = ()
    cache_preserved: bool = False
    production_authorization_available: Literal[False] = False
    submission_allowed: Literal[False] = False
    execution_authority: Literal[False] = False
