"""Read-only deterministic historical-validation API."""

from fastapi import APIRouter, Depends

from app.models.backtesting import BacktestRequest, BacktestResponse
from app.services.backtesting import BacktestingService, get_backtesting_service


router = APIRouter(tags=["historical-validation"])


@router.post("/backtests", response_model=BacktestResponse)
def run_backtest(
    request: BacktestRequest,
    service: BacktestingService = Depends(get_backtesting_service),
) -> BacktestResponse:
    """Evaluate cached evidence without fetching data or causing side effects."""
    return service.run(request)
