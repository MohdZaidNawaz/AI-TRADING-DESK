"""Immutable provider-neutral issuer and listed-instrument reference contracts.

Reference profiles retain only source-reported identity and classification context.
They deliberately do not perform entity resolution, valuation, alias matching, or
derive market, risk, or trading conclusions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json

from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.events import AssetClass, DataDomain, SourceTransport
from app.models.schemas import APIModel


class IssuerReferenceModel(APIModel):
    """Frozen base for issuer-reference boundary contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentKind(str, Enum):
    COMMON_EQUITY = "common_equity"
    PREFERRED_EQUITY = "preferred_equity"
    ETF = "etf"
    FUND = "fund"
    ADR = "adr"
    DEPOSITARY_RECEIPT = "depositary_receipt"
    OTHER = "other"
    UNKNOWN = "unknown"


class IssuerReferenceStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class IssuerReferenceSource(IssuerReferenceModel):
    """Immutable identity of a source that reports reference-profile facts."""

    source_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    transport: SourceTransport
    domain: DataDomain
    origin_id: str | None = Field(default=None, max_length=80)
    origin_name: str | None = Field(default=None, max_length=120)

    @field_validator("source_id", "display_name", "origin_id", "origin_name", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_source(self) -> "IssuerReferenceSource":
        if self.domain is not DataDomain.FUNDAMENTAL_DATA:
            raise ValueError("issuer-reference sources must use the fundamental_data domain")
        if self.source_id is None or self.display_name is None:  # pragma: no cover - Field guards
            raise ValueError("issuer-reference source identity values cannot be blank")
        if (self.origin_id is None) != (self.origin_name is None):
            raise ValueError("origin_id and origin_name must be supplied together")
        return self


class IssuerInstrumentReferenceKey(IssuerReferenceModel):
    """Source-scoped issuer/listing identity; it intentionally performs no matching."""

    source_id: str = Field(min_length=1, max_length=80)
    source_issuer_id: str = Field(min_length=1, max_length=160)
    source_instrument_id: str = Field(min_length=1, max_length=160)
    source_symbol: str | None = Field(default=None, max_length=40)
    venue: str | None = Field(default=None, max_length=80)

    @field_validator("source_id", "source_issuer_id", "source_instrument_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("issuer-reference identity values cannot be blank")
            return normalized
        return value

    @field_validator("source_symbol", "venue", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @computed_field(return_type=str)
    @property
    def instrument_id(self) -> str:
        return _stable_id(
            "issuer-reference-instrument",
            self.source_id,
            self.source_issuer_id,
            self.source_instrument_id,
            self.source_symbol or "",
            self.venue or "",
        )


class IssuerClassification(IssuerReferenceModel):
    """Explicit source taxonomy context without inferred mappings."""

    taxonomy_id: str = Field(min_length=1, max_length=120)
    taxonomy_name: str = Field(min_length=1, max_length=160)
    sector: str | None = Field(default=None, max_length=160)
    industry: str | None = Field(default=None, max_length=160)

    @field_validator("taxonomy_id", "taxonomy_name", "sector", "industry", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value


class NormalizedIssuerInstrumentReference(IssuerReferenceModel):
    """One immutable source-issued issuer/listing profile version.

    Connectors must normalize source payloads before this model is constructed.
    Raw provider payloads and locally inferred identity fields are intentionally absent.
    """

    source: IssuerReferenceSource
    instrument: IssuerInstrumentReferenceKey
    source_profile_id: str = Field(min_length=1, max_length=160)
    issuer_display_name: str = Field(min_length=1, max_length=240)
    issuer_legal_name: str | None = Field(default=None, max_length=240)
    instrument_display_name: str | None = Field(default=None, max_length=240)
    asset_class: AssetClass = AssetClass.UNKNOWN
    instrument_kind: InstrumentKind = InstrumentKind.UNKNOWN
    status: IssuerReferenceStatus = IssuerReferenceStatus.UNKNOWN
    classification: IssuerClassification | None = None
    country: str | None = Field(default=None, max_length=80)
    domicile_country: str | None = Field(default=None, max_length=80)
    reference_currency: str | None = Field(default=None, max_length=12)
    as_of: datetime
    published_at: datetime | None = None
    profile_version: int = Field(ge=1)
    revision_of_profile_id: str | None = Field(default=None, max_length=160)
    received_at: datetime

    @field_validator(
        "source_profile_id",
        "issuer_display_name",
        "issuer_legal_name",
        "instrument_display_name",
        "country",
        "domicile_country",
        "reference_currency",
        "revision_of_profile_id",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("reference_currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("as_of", "published_at", "received_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issuer-reference timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_reference(self) -> "NormalizedIssuerInstrumentReference":
        if self.source.source_id != self.instrument.source_id:
            raise ValueError("issuer-reference source_id must match the instrument key")
        if self.profile_version == 1 and self.revision_of_profile_id is not None:
            raise ValueError("version-one issuer profiles cannot declare a revision link")
        if self.profile_version > 1 and self.revision_of_profile_id is None:
            raise ValueError("revised issuer profiles require revision_of_profile_id")
        return self

    @computed_field(return_type=str)
    @property
    def fingerprint(self) -> str:
        """Semantic profile fingerprint; local receipt time is intentionally excluded."""
        return _fingerprint(
            {
                "source": _canonical_source(self.source),
                "instrument": _canonical_instrument(self.instrument),
                "source_profile_id": self.source_profile_id,
                "issuer_display_name": self.issuer_display_name,
                "issuer_legal_name": self.issuer_legal_name,
                "instrument_display_name": self.instrument_display_name,
                "asset_class": self.asset_class.value,
                "instrument_kind": self.instrument_kind.value,
                "status": self.status.value,
                "classification": _canonical_classification(self.classification),
                "country": self.country,
                "domicile_country": self.domicile_country,
                "reference_currency": self.reference_currency,
                "as_of": self.as_of.isoformat(),
                "published_at": self.published_at.isoformat() if self.published_at else None,
                "profile_version": self.profile_version,
                "revision_of_profile_id": self.revision_of_profile_id,
            }
        )

    @computed_field(return_type=str)
    @property
    def profile_id(self) -> str:
        return "issuer-reference-profile-" + _fingerprint(
            {
                "source_id": self.source.source_id,
                "instrument_id": self.instrument.instrument_id,
                "source_profile_id": self.source_profile_id,
                "profile_version": self.profile_version,
                "fingerprint": self.fingerprint,
            }
        )


def _canonical_source(source: IssuerReferenceSource) -> dict[str, str | None]:
    return {
        "source_id": source.source_id,
        "display_name": source.display_name,
        "transport": source.transport.value,
        "domain": source.domain.value,
        "origin_id": source.origin_id,
        "origin_name": source.origin_name,
    }


def _canonical_instrument(key: IssuerInstrumentReferenceKey) -> dict[str, str | None]:
    return {
        "source_id": key.source_id,
        "source_issuer_id": key.source_issuer_id,
        "source_instrument_id": key.source_instrument_id,
        "source_symbol": key.source_symbol,
        "venue": key.venue,
    }


def _canonical_classification(
    classification: IssuerClassification | None,
) -> dict[str, str | None] | None:
    if classification is None:
        return None
    return {
        "taxonomy_id": classification.taxonomy_id,
        "taxonomy_name": classification.taxonomy_name,
        "sector": classification.sector,
        "industry": classification.industry,
    }


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()}"


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()
