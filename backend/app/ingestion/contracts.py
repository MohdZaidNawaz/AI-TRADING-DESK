"""Interfaces that isolate source connectors from FRIDAY Core."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.models.events import NormalizedDataEvent, SourceMetadata


@runtime_checkable
class SourceConnector(Protocol):
    """Converts one provider's payload into a normalized internal event."""

    source: SourceMetadata

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedDataEvent:
        """Validate and normalize provider-specific input without side effects."""


class EventConsumer(Protocol):
    """Consumes normalized events without knowing their provider payload format."""

    def ingest_event(self, event: NormalizedDataEvent) -> object:
        """Pass an event to the intelligence orchestration layer."""
