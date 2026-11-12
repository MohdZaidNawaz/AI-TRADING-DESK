"""Sanitized read-only LLM provider catalogue."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.llm.budget import LLMBudgetError
from app.llm.registry import LLMProviderRegistry, get_llm_provider_registry
from app.models.llm_synthesis import (
    LLMBudgetStatus,
    LLMProviderCatalogue,
    LLMUsageRecord,
)

router = APIRouter(prefix="/llm", tags=["llm"])


@router.get("/providers", response_model=LLMProviderCatalogue)
def providers(
    registry: LLMProviderRegistry = Depends(get_llm_provider_registry),
) -> LLMProviderCatalogue:
    return registry.catalogue()


@router.get("/budget", response_model=LLMBudgetStatus)
def budget(
    registry: LLMProviderRegistry = Depends(get_llm_provider_registry),
) -> LLMBudgetStatus:
    try:
        return registry.budget_ledger.status().model_copy(
            update={"provider_pricing": registry.pricing_status()}
        )
    except LLMBudgetError:
        raise HTTPException(status_code=503, detail="LLM budget ledger unavailable") from None


@router.get("/usage", response_model=tuple[LLMUsageRecord, ...])
def usage(
    limit: int = Query(default=100, ge=1, le=500),
    registry: LLMProviderRegistry = Depends(get_llm_provider_registry),
) -> tuple[LLMUsageRecord, ...]:
    try:
        return registry.budget_ledger.usage(limit=limit)
    except LLMBudgetError:
        raise HTTPException(status_code=503, detail="LLM budget ledger unavailable") from None
