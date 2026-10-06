"""Internal provider-neutral ingestion boundary for portfolio-risk snapshots."""

from __future__ import annotations

from functools import lru_cache

from app.ingestion.contracts import PortfolioRiskConsumer
from app.models.portfolio_risk import NormalizedPortfolioRiskSnapshot
from app.services.friday_core import get_friday_core


class PortfolioRiskIngestionService:
    """Stage validated portfolio context without fetching, calculation, or execution."""

    def __init__(self, *, consumer: PortfolioRiskConsumer) -> None:
        self._consumer = consumer

    def ingest(self, snapshot: NormalizedPortfolioRiskSnapshot) -> object:
        if not isinstance(snapshot, NormalizedPortfolioRiskSnapshot):
            raise TypeError("snapshot must be a NormalizedPortfolioRiskSnapshot")
        return self._consumer.ingest_portfolio_risk_snapshot(snapshot)

    def ingest_many(
        self,
        snapshots: tuple[NormalizedPortfolioRiskSnapshot, ...],
    ) -> tuple[object, ...]:
        return tuple(self.ingest(snapshot) for snapshot in snapshots)


@lru_cache
def get_portfolio_risk_ingestion_service() -> PortfolioRiskIngestionService:
    """Return the process-local boundary; no public route uses it."""
    return PortfolioRiskIngestionService(consumer=get_friday_core())
