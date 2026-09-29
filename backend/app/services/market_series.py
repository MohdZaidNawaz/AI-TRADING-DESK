"""Internal provider-neutral ingestion boundary for historical market bars."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import MarketBarConsumer
from app.models.market_series import NormalizedMarketBar
from app.services.friday_core import get_friday_core


class MarketSeriesIngestionService:
    """Stage validated bars without fetching data or triggering analysis."""

    def __init__(self, *, consumer: MarketBarConsumer) -> None:
        self._consumer = consumer

    def ingest(self, bar: NormalizedMarketBar) -> object:
        if not isinstance(bar, NormalizedMarketBar):
            raise TypeError("bar must be a NormalizedMarketBar")
        return self._consumer.ingest_market_bar(bar)

    def ingest_many(self, bars: tuple[NormalizedMarketBar, ...]) -> tuple[object, ...]:
        return tuple(self.ingest(bar) for bar in bars)


@lru_cache
def get_market_series_ingestion_service() -> MarketSeriesIngestionService:
    """Return the process-local historical-series boundary; no API route uses it."""
    return MarketSeriesIngestionService(consumer=get_friday_core())
