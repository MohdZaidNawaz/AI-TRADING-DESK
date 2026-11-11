"""Sanitized read-only LLM provider catalogue."""

from fastapi import APIRouter, Depends

from app.llm.registry import LLMProviderRegistry, get_llm_provider_registry
from app.models.llm_synthesis import LLMProviderCatalogue

router = APIRouter(prefix="/llm", tags=["llm"])


@router.get("/providers", response_model=LLMProviderCatalogue)
def providers(
    registry: LLMProviderRegistry = Depends(get_llm_provider_registry),
) -> LLMProviderCatalogue:
    return registry.catalogue()
