"""Provider-neutral ingestion contracts and services."""

from app.ingestion.service import (
    IngestionService,
    MarketSnapshotService,
    NewsResearchIngestionService,
)
from app.ingestion.contracts import (
    EconomicEventConsumer,
    CorporateActionConsumer,
    CorporateActionConnector,
    EstimateObservationConsumer,
    EstimateSeriesConnector,
    FundamentalObservationConsumer,
    FundamentalSeriesConnector,
    MacroObservationConsumer,
    MacroSeriesConnector,
    NewsEvidenceConsumer,
    PositioningConnector,
    PositioningObservationConsumer,
)

__all__ = [
    "IngestionService",
    "MarketSnapshotService",
    "NewsResearchIngestionService",
    "NewsEvidenceConsumer",
    "EconomicEventConsumer",
    "CorporateActionConsumer",
    "CorporateActionConnector",
    "EstimateObservationConsumer",
    "EstimateSeriesConnector",
    "FundamentalObservationConsumer",
    "FundamentalSeriesConnector",
    "MacroObservationConsumer",
    "MacroSeriesConnector",
    "PositioningConnector",
    "PositioningObservationConsumer",
]
