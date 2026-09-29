"""Interfaces that isolate source connectors from FRIDAY Core."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.models.events import (
    NormalizedDataEvent,
    NormalizedNewsResearchEvent,
    SourceMetadata,
)


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


class MarketDataClient(Protocol):
    """Retrieves a provider payload without exposing provider details downstream."""

    async def fetch_snapshot(self, symbol: str) -> Mapping[str, Any]:
        """Return one raw market snapshot for connector normalization."""


@runtime_checkable
class NewsResearchConnector(Protocol):
    """Converts one provider payload into a normalized news/research event."""

    source: SourceMetadata

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedNewsResearchEvent:
        """Keep provider-specific parsing outside FRIDAY Core."""
