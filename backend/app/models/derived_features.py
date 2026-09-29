"""Immutable provider-neutral contracts for deterministic derived market features."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import math

from pydantic import ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.events import AssetClass
from app.models.schemas import APIModel


class DerivedFeatureModel(APIModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeatureType(str, Enum):
    BAR_RANGE = "bar_range"
    AVERAGE_RANGE = "average_range"
    SIMPLE_RETURN = "simple_return"
    LOG_RETURN = "log_return"
    SMA = "sma"
    ROC = "roc"
    ROLLING_STD = "rolling_std"


class FeatureStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT_DATA = "insufficient_data"
    UNDEFINED = "undefined"
    REJECTED_INPUT = "rejected_input"


class FeatureQualityFlag(str, Enum):
    GAP_DETECTED = "gap_detected"
    PROVISIONAL_EXCLUDED = "provisional_excluded"
    EARLIER_GAP_EXCLUDED = "earlier_gap_excluded"
    STALE_INPUT_UNASSESSED = "stale_input_unassessed"
    FLAT_SERIES = "flat_series"


class FeatureValueUnit(str, Enum):
    PRICE = "price"
    RATIO = "ratio"


class FeatureParameter(DerivedFeatureModel):
    """One canonically ordered, immutable calculation parameter."""

    name: str = Field(min_length=1, max_length=80)
    value: int | FiniteFloat | str

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                raise ValueError("feature parameter names cannot be blank")
            return normalized
        return value

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean feature parameters are not supported")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("numeric feature parameters must be finite")
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("string feature parameters cannot be blank")
            return normalized
        return value


class DerivedFeatureRequest(DerivedFeatureModel):
    """An internal calculation request; it is not an API request model."""

    feature_type: FeatureType
    parameters: tuple[FeatureParameter, ...] = ()

    @field_validator("parameters")
    @classmethod
    def canonicalize_parameters(
        cls,
        values: tuple[FeatureParameter, ...],
    ) -> tuple[FeatureParameter, ...]:
        names = [value.name for value in values]
        if len(names) != len(set(names)):
            raise ValueError("feature parameter names must be unique")
        return tuple(sorted(values, key=lambda item: (item.name, repr(item.value))))


class DerivedMarketFeature(DerivedFeatureModel):
    """One derived value with complete raw-input and calculation lineage."""

    feature_id: str = Field(min_length=1, max_length=120)
    feature_type: FeatureType
    status: FeatureStatus
    symbol: str = Field(min_length=1, max_length=40)
    asset_class: AssetClass
    venue: str | None = Field(default=None, max_length=80)
    timeframe: str = Field(min_length=1, max_length=20)
    as_of: datetime | None = None
    value: FiniteFloat | None = None
    value_unit: FeatureValueUnit
    parameters: tuple[FeatureParameter, ...] = ()
    method_id: str = Field(min_length=1, max_length=120)
    method_version: str = Field(min_length=1, max_length=40)
    input_series_id: str = Field(min_length=1, max_length=120)
    input_series_fingerprint: str = Field(min_length=64, max_length=64)
    input_first_bar_id: str | None = Field(default=None, max_length=120)
    input_last_bar_id: str | None = Field(default=None, max_length=120)
    input_bar_count: int = Field(ge=0)
    required_bar_count: int = Field(ge=1)
    completed_only: bool = True
    quality_flags: tuple[FeatureQualityFlag, ...] = ()
    limitations: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("feature as_of must include a timezone")
        return value.astimezone(UTC)

    @field_validator("parameters")
    @classmethod
    def canonicalize_parameters(
        cls,
        values: tuple[FeatureParameter, ...],
    ) -> tuple[FeatureParameter, ...]:
        names = [value.name for value in values]
        if len(names) != len(set(names)):
            raise ValueError("feature parameter names must be unique")
        return tuple(sorted(values, key=lambda item: (item.name, repr(item.value))))

    @field_validator("quality_flags")
    @classmethod
    def canonicalize_quality_flags(
        cls,
        values: tuple[FeatureQualityFlag, ...],
    ) -> tuple[FeatureQualityFlag, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))

    @field_validator("limitations")
    @classmethod
    def canonicalize_limitations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = (value.strip() for value in values if value.strip())
        return tuple(sorted(set(normalized)))

    @model_validator(mode="after")
    def validate_result_contract(self) -> "DerivedMarketFeature":
        if self.status is FeatureStatus.AVAILABLE:
            if self.value is None:
                raise ValueError("available features require a finite value")
            if self.input_bar_count < self.required_bar_count:
                raise ValueError("available features require sufficient input bars")
        elif self.value is not None:
            raise ValueError("unavailable features cannot contain a value")

        has_input_range = self.input_bar_count > 0
        if has_input_range and (
            self.input_first_bar_id is None
            or self.input_last_bar_id is None
            or self.as_of is None
        ):
            raise ValueError("features with input bars require complete input range metadata")
        if not has_input_range and any(
            value is not None
            for value in (self.input_first_bar_id, self.input_last_bar_id, self.as_of)
        ):
            raise ValueError("features without input bars cannot contain input range metadata")
        return self


class DerivedFeatureSnapshot(DerivedFeatureModel):
    """One deterministic immutable feature set shared by an analysis run."""

    snapshot_id: str = Field(min_length=1, max_length=120)
    features: tuple[DerivedMarketFeature, ...] = ()

    @field_validator("features")
    @classmethod
    def canonicalize_features(
        cls,
        values: tuple[DerivedMarketFeature, ...],
    ) -> tuple[DerivedMarketFeature, ...]:
        unique = {feature.feature_id: feature for feature in values}
        if len(unique) != len(values):
            raise ValueError("derived feature IDs must be unique within a snapshot")
        return tuple(unique[key] for key in sorted(unique))


def feature_identity(
    *,
    feature_type: FeatureType,
    parameters: tuple[FeatureParameter, ...],
    method_id: str,
    method_version: str,
    input_series_id: str,
    input_series_fingerprint: str,
    input_first_bar_id: str | None,
    input_last_bar_id: str | None,
) -> str:
    """Return the stable semantic identity required by the Phase 5B-1 contract."""
    canonical_parameters = tuple(
        sorted(parameters, key=lambda item: (item.name, repr(item.value)))
    )
    payload = {
        "feature_type": feature_type.value,
        "parameters": [item.model_dump(mode="json") for item in canonical_parameters],
        "method_id": method_id,
        "method_version": method_version,
        "input_series_id": input_series_id,
        "input_series_fingerprint": input_series_fingerprint,
        "input_first_bar_id": input_first_bar_id,
        "input_last_bar_id": input_last_bar_id,
    }
    return "feature-" + _fingerprint(payload)


def feature_snapshot_identity(features: tuple[DerivedMarketFeature, ...]) -> str:
    return "feature-snapshot-" + _fingerprint(
        [feature.feature_id for feature in sorted(features, key=lambda item: item.feature_id)]
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
