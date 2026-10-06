"""Internal provider-neutral ingestion boundary for issuer reference profiles."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import IssuerReferenceConsumer
from app.models.issuer_reference import NormalizedIssuerInstrumentReference
from app.services.friday_core import get_friday_core


class IssuerReferenceIngestionService:
    """Stage validated reference profiles without fetching or interpreting data."""

    def __init__(self, *, consumer: IssuerReferenceConsumer) -> None:
        self._consumer = consumer

    def ingest(self, profile: NormalizedIssuerInstrumentReference) -> object:
        if not isinstance(profile, NormalizedIssuerInstrumentReference):
            raise TypeError("profile must be a NormalizedIssuerInstrumentReference")
        return self._consumer.ingest_issuer_reference(profile)

    def ingest_many(
        self,
        profiles: tuple[NormalizedIssuerInstrumentReference, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(profile) for profile in profiles)


@lru_cache
def get_issuer_reference_ingestion_service() -> IssuerReferenceIngestionService:
    """Return the process-local boundary; no public route uses it."""
    return IssuerReferenceIngestionService(consumer=get_friday_core())
