from datetime import UTC, datetime

from fastapi import APIRouter, Response

from app.models.production_authorization import ProductionAuthorizationReadinessReport
from app.models.schemas import HealthResponse
from app.services.production_authorization_readiness import (
    ProductionAuthorizationReadinessGate,
)

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def get_health(response: Response) -> HealthResponse:
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        status="ok",
        service="nimbus-desk-api",
        version="0.2.0",
    )


@router.get("/readiness", response_model=ProductionAuthorizationReadinessReport)
def get_readiness(response: Response) -> ProductionAuthorizationReadinessReport:
    """Return a read-only, structurally non-authorizing readiness diagnostic."""

    response.headers["Cache-Control"] = "no-store"
    return ProductionAuthorizationReadinessGate().evaluate(now=datetime.now(UTC))
