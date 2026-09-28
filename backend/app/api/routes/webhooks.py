import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.connectors.tradingview import (
    TradingViewPayloadError,
    normalize_tradingview_payload,
    require_tradingview_token,
)
from app.models.schemas import WebhookAcceptedResponse
from app.services.ingestion import get_ingestion_service

router = APIRouter(prefix="/webhooks/tradingview", tags=["webhooks"])


@router.post("/{webhook_token}", response_model=WebhookAcceptedResponse, status_code=status.HTTP_200_OK)
async def receive_tradingview_webhook(
    webhook_token: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> WebhookAcceptedResponse:
    """Authenticate, normalize, and defer a TradingView alert for FRIDAY Core."""
    require_tradingview_token(webhook_token)

    try:
        payload: Any = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body must be valid JSON.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Webhook payload must be a JSON object.",
        )

    try:
        event = normalize_tradingview_payload(payload)
    except TradingViewPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    background_tasks.add_task(get_ingestion_service().ingest, event)
    return WebhookAcceptedResponse(
        status="accepted",
        event_id=event.event_id,
        source=event.source.source_id,
    )
