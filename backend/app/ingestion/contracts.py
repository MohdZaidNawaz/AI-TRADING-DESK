"""Interfaces that isolate source connectors from FRIDAY Core."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.models.events import (
    NormalizedDataEvent,
    NormalizedNewsResearchEvent,
    SourceMetadata,
)
from app.models.market_series import NormalizedMarketBar
from app.models.economic_events import NormalizedEconomicEvent
from app.models.macro_series import MacroSeriesSource, NormalizedMacroObservation
from app.models.positioning import PositioningSource, NormalizedPositioningObservation
from app.models.fundamentals import FundamentalSource, NormalizedFundamentalObservation
from app.models.estimates import EstimateSource, NormalizedEstimateObservation
from app.models.corporate_actions import CorporateActionSource, NormalizedCorporateAction


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


class NewsEvidenceConsumer(Protocol):
    """Stages normalized news/research evidence without provider-specific parsing."""

    def ingest_news_event(self, event: NormalizedNewsResearchEvent) -> object:
        """Pass a normalized news item to the shared evidence layer."""


@runtime_checkable
class MarketBarConsumer(Protocol):
    """Stages validated historical bars without provider-specific behavior."""

    def ingest_market_bar(self, bar: NormalizedMarketBar) -> object:
        """Pass one normalized bar to the dedicated series evidence store."""


@runtime_checkable
class EconomicEventConsumer(Protocol):
    """Stages normalized economic events without provider-specific behavior."""

    def ingest_economic_event(self, event: NormalizedEconomicEvent) -> object:
        """Pass one immutable lifecycle version to the economic evidence store."""


@runtime_checkable
class MacroObservationConsumer(Protocol):
    """Stages normalized macro observations without provider-specific parsing."""

    def ingest_macro_observation(self, observation: NormalizedMacroObservation) -> object:
        """Pass one immutable macro observation to the dedicated series store."""


@runtime_checkable
class PositioningObservationConsumer(Protocol):
    """Stages reported positioning evidence without provider-specific parsing."""

    def ingest_positioning_observation(
        self,
        observation: NormalizedPositioningObservation,
    ) -> object:
        """Pass one immutable observation to the positioning evidence store."""


@runtime_checkable
class FundamentalObservationConsumer(Protocol):
    """Stages financial-statement evidence without provider-specific parsing."""

    def ingest_fundamental_observation(
        self,
        observation: NormalizedFundamentalObservation,
    ) -> object:
        """Pass one immutable observation to the fundamental evidence store."""


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


@runtime_checkable
class MacroSeriesConnector(Protocol):
    """Future connector boundary for one normalized macro observation."""

    source: MacroSeriesSource

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedMacroObservation:
        """Keep source parsing outside the macro evidence and agent layers."""


@runtime_checkable
class PositioningConnector(Protocol):
    """Future connector boundary for one normalized positioning observation."""

    source: PositioningSource

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedPositioningObservation:
        """Keep source parsing outside positioning evidence and agent layers."""


@runtime_checkable
class FundamentalSeriesConnector(Protocol):
    """Future connector boundary for one normalized financial observation."""

    source: FundamentalSource

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedFundamentalObservation:
        """Keep source parsing outside fundamental evidence and agent layers."""


@runtime_checkable
class EstimateObservationConsumer(Protocol):
    """Stages source-reported estimates without provider-specific parsing."""

    def ingest_estimate_observation(
        self,
        observation: NormalizedEstimateObservation,
    ) -> object:
        """Pass one immutable estimate observation to the dedicated estimate store."""


@runtime_checkable
class EstimateSeriesConnector(Protocol):
    """Future connector boundary for one normalized estimate observation."""

    source: EstimateSource

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedEstimateObservation:
        """Keep source parsing outside estimate evidence and agent layers."""


@runtime_checkable
class CorporateActionConsumer(Protocol):
    """Stages normalized source-reported corporate actions without provider parsing."""

    def ingest_corporate_action(self, action: NormalizedCorporateAction) -> object:
        """Pass one immutable action version to its dedicated evidence store."""


@runtime_checkable
class CorporateActionConnector(Protocol):
    """Future connector boundary for one normalized corporate action."""

    source: CorporateActionSource

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedCorporateAction:
        """Keep source parsing outside corporate-action evidence and agents."""
