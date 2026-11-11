"""Configuration-backed provider registry and deterministic selection."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import httpx

from app.config import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MODEL_ENV,
    GEMINI_API_KEY_ENV,
    GEMINI_MODEL_ENV,
    LLM_DEFAULT_PROVIDER_ENV,
    LLM_MANUAL_FALLBACK_ENV,
    LLM_MAX_OUTPUT_TOKENS_ENV,
    LLM_PROVIDER_MODE_ENV,
    LLM_TIMEOUT_SECONDS_ENV,
    OPENAI_API_KEY_ENV,
    OPENAI_MODEL_ENV,
    get_llm_environment,
)
from app.llm.contracts import LLMProvider
from app.llm.providers import AnthropicProvider, GeminiProvider, OpenAIProvider
from app.models.llm_synthesis import (
    LLMProviderCatalogue,
    LLMProviderCatalogueEntry,
    LLMProviderMode,
    LLMProviderName,
)

_ORDER: tuple[LLMProviderName, ...] = ("openai", "anthropic", "gemini")
_DEFAULT_MODELS: dict[LLMProviderName, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-5-20250929",
    "gemini": "gemini-2.5-flash",
}


class LLMSelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LLMSettings:
    mode: LLMProviderMode
    default_provider: LLMProviderName
    manual_fallback_enabled: bool
    timeout_seconds: float
    max_output_tokens: int


class LLMProviderRegistry:
    def __init__(
        self,
        *,
        environment: dict[str, str | None] | None = None,
        transports: dict[LLMProviderName, httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        env = environment if environment is not None else get_llm_environment()
        mode = _choice(env.get(LLM_PROVIDER_MODE_ENV), ("auto", "manual"), "auto")
        default = _choice(
            env.get(LLM_DEFAULT_PROVIDER_ENV), _ORDER, "openai"
        )
        self.settings = LLMSettings(
            mode=mode,  # type: ignore[arg-type]
            default_provider=default,  # type: ignore[arg-type]
            manual_fallback_enabled=_strict_bool(env.get(LLM_MANUAL_FALLBACK_ENV)),
            timeout_seconds=_bounded_float(
                env.get(LLM_TIMEOUT_SECONDS_ENV), default=20.0, low=1.0, high=60.0
            ),
            max_output_tokens=_bounded_int(
                env.get(LLM_MAX_OUTPUT_TOKENS_ENV), default=1600, low=256, high=4096
            ),
        )
        transports = transports or {}
        definitions = (
            ("openai", OPENAI_API_KEY_ENV, OPENAI_MODEL_ENV, OpenAIProvider),
            ("anthropic", ANTHROPIC_API_KEY_ENV, ANTHROPIC_MODEL_ENV, AnthropicProvider),
            ("gemini", GEMINI_API_KEY_ENV, GEMINI_MODEL_ENV, GeminiProvider),
        )
        self._providers: dict[LLMProviderName, LLMProvider] = {}
        self._health: dict[LLMProviderName, Literal["unknown", "healthy", "unavailable"]] = {
            name: "unknown" for name in _ORDER
        }
        self._configured_models: dict[LLMProviderName, str | None] = {
            name: None for name in _ORDER
        }
        for name, key_env, model_env, provider_type in definitions:
            key = env.get(key_env)
            if not key:
                continue
            model = env.get(model_env) or _DEFAULT_MODELS[name]
            self._configured_models[name] = model
            self._providers[name] = provider_type(
                api_key=key,
                model=model,
                timeout_seconds=self.settings.timeout_seconds,
                transport=transports.get(name),
            )

    def validate_manual(self, provider: LLMProviderName | None, model: str | None) -> None:
        if provider is None:
            raise LLMSelectionError("manual mode requires an explicit provider")
        selected = self._providers.get(provider)
        if selected is None:
            raise LLMSelectionError("selected provider is not configured")
        if model is not None and model != selected.model:
            raise LLMSelectionError("selected model is not in the configured catalogue")

    def candidates(
        self,
        *,
        mode: LLMProviderMode,
        provider: LLMProviderName | None,
        model: str | None,
    ) -> tuple[LLMProvider, ...]:
        if mode == "manual":
            self.validate_manual(provider, model)
            primary = self._providers[provider]  # type: ignore[index]
            if not self.settings.manual_fallback_enabled:
                return (primary,)
            return (primary,) + tuple(
                self._providers[name]
                for name in self._auto_order()
                if name != provider
            )
        return tuple(self._providers[name] for name in self._auto_order())

    def mark_healthy(self, provider: LLMProviderName) -> None:
        self._health[provider] = "healthy"

    def mark_unavailable(self, provider: LLMProviderName) -> None:
        self._health[provider] = "unavailable"

    def catalogue(self) -> LLMProviderCatalogue:
        return LLMProviderCatalogue(
            configured_mode=self.settings.mode,
            manual_fallback_enabled=self.settings.manual_fallback_enabled,
            providers=tuple(
                LLMProviderCatalogueEntry(
                    provider=name,
                    configured=name in self._providers,
                    available=name in self._providers
                    and self._health[name] == "healthy",
                    health=self._health[name],
                    model=self._configured_models[name],
                )
                for name in _ORDER
            ),
        )

    def _auto_order(self) -> tuple[LLMProviderName, ...]:
        default = self.settings.default_provider
        ordered = (default,) + tuple(name for name in _ORDER if name != default)
        # Runtime health is diagnostic, not a permanent circuit breaker.
        # A transient failure must not remove a configured provider from future
        # analyses; each new request gets a chance to recover.
        return tuple(name for name in ordered if name in self._providers)


def _choice(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    normalized = (value or default).strip().lower()
    return normalized if normalized in allowed else default


def _strict_bool(value: str | None) -> bool:
    return value is not None and value.strip().casefold() == "true"


def _bounded_float(value: str | None, *, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except ValueError:
        return default
    return min(max(parsed, low), high)


def _bounded_int(value: str | None, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return min(max(parsed, low), high)


@lru_cache(maxsize=1)
def get_llm_provider_registry() -> LLMProviderRegistry:
    return LLMProviderRegistry()
