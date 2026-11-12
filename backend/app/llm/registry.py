"""Configuration-backed provider registry and deterministic selection."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx

from app.config import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MODEL_ENV,
    GEMINI_API_KEY_ENV,
    GEMINI_MODEL_ENV,
    GROQ_API_KEY_ENV,
    GROQ_MODEL_ENV,
    LLM_DEFAULT_PROVIDER_ENV,
    LLM_BUDGET_DB_PATH_ENV,
    LLM_INR_PER_USD_ENV,
    LLM_MANUAL_FALLBACK_ENV,
    LLM_MAX_OUTPUT_TOKENS_ENV,
    LLM_MAX_ATTEMPTS_ENV,
    LLM_RETRY_BASE_SECONDS_ENV,
    LLM_MAX_RETRY_DELAY_SECONDS_ENV,
    LLM_PROVIDER_MODE_ENV,
    LLM_PROVIDER_PRICING_JSON_ENV,
    LLM_TIMEOUT_SECONDS_ENV,
    OPENAI_API_KEY_ENV,
    OPENAI_MODEL_ENV,
    get_llm_environment,
)
from app.llm.contracts import LLMProvider
from app.llm.budget import (
    BudgetAuthorization,
    LLMBudgetError,
    LLMBudgetLedger,
    ProviderPricing,
    budget_policy_from_environment,
    parse_provider_pricing,
)
from app.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    OpenAIProvider,
)
from app.models.llm_synthesis import (
    LLMFallbackReason,
    LLMProviderCatalogue,
    LLMProviderCatalogueEntry,
    LLMProviderPricingStatus,
    LLMProviderMode,
    LLMProviderName,
)

_ORDER: tuple[LLMProviderName, ...] = ("openai", "anthropic", "gemini", "groq")
_DEFAULT_MODELS: dict[LLMProviderName, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-5-20250929",
    "gemini": "gemini-3.8-flash",
    "groq": "qwen/qwen3.8-27b",
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
    max_attempts: int
    retry_base_seconds: float
    max_retry_delay_seconds: float


class LLMProviderRegistry:
    def __init__(
        self,
        *,
        environment: dict[str, str | None] | None = None,
        transports: dict[LLMProviderName, httpx.AsyncBaseTransport] | None = None,
        budget_ledger: LLMBudgetLedger | None = None,
    ) -> None:
        env = environment if environment is not None else get_llm_environment()
        mode = _choice(env.get(LLM_PROVIDER_MODE_ENV), ("auto", "manual"), "auto")
        default = _choice(
            env.get(LLM_DEFAULT_PROVIDER_ENV), _ORDER, "groq"
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
            max_attempts=_bounded_int(
                env.get(LLM_MAX_ATTEMPTS_ENV), default=2, low=1, high=3
            ),
            retry_base_seconds=_bounded_float(
                env.get(LLM_RETRY_BASE_SECONDS_ENV), default=0.25, low=0.0, high=5.0
            ),
            max_retry_delay_seconds=_bounded_float(
                env.get(LLM_MAX_RETRY_DELAY_SECONDS_ENV), default=10.0, low=0.0, high=60.0
            ),
        )
        transports = transports or {}
        definitions = (
            ("openai", OPENAI_API_KEY_ENV, OPENAI_MODEL_ENV, OpenAIProvider),
            ("anthropic", ANTHROPIC_API_KEY_ENV, ANTHROPIC_MODEL_ENV, AnthropicProvider),
            ("gemini", GEMINI_API_KEY_ENV, GEMINI_MODEL_ENV, GeminiProvider),
            ("groq", GROQ_API_KEY_ENV, GROQ_MODEL_ENV, GroqProvider),
        )
        self._providers: dict[LLMProviderName, LLMProvider] = {}
        self._health: dict[LLMProviderName, Literal[
            "unknown", "healthy", "temporarily_unavailable", "authentication_failed", "configuration_error"
        ]] = {
            name: "unknown" for name in _ORDER
        }
        self._last_failure: dict[LLMProviderName, LLMFallbackReason | None] = {
            name: None for name in _ORDER
        }
        self._configured_models: dict[LLMProviderName, str | None] = {
            name: None for name in _ORDER
        }
        self._pricing = parse_provider_pricing(
            env.get(LLM_PROVIDER_PRICING_JSON_ENV),
            inr_per_usd=_optional_decimal(env.get(LLM_INR_PER_USD_ENV)),
        )
        self._budget_ledger: LLMBudgetLedger | None = budget_ledger
        self._budget_error: LLMBudgetError | None = None
        if self._budget_ledger is None:
            configured_path = env.get(LLM_BUDGET_DB_PATH_ENV)
            backend_root = Path(__file__).resolve().parents[2]
            path = Path(configured_path) if configured_path else Path("runtime/llm_usage.sqlite3")
            if not path.is_absolute():
                path = backend_root / path
            try:
                self._budget_ledger = LLMBudgetLedger(
                    path, budget_policy_from_environment(env)
                )
            except (LLMBudgetError, ValueError) as error:
                self._budget_error = LLMBudgetError("LLM budget ledger is unavailable")
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
                max_attempts=self.settings.max_attempts,
                retry_base_seconds=self.settings.retry_base_seconds,
                max_retry_delay_seconds=self.settings.max_retry_delay_seconds,
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
        self._last_failure[provider] = None

    def mark_failed(
        self,
        provider: LLMProviderName,
        reason: LLMFallbackReason,
    ) -> None:
        if reason == "authentication":
            self._health[provider] = "authentication_failed"
        elif reason in ("invalid_request", "malformed_response", "schema_validation"):
            self._health[provider] = "configuration_error"
        else:
            self._health[provider] = "temporarily_unavailable"
        self._last_failure[provider] = reason

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
                    last_failure_reason=self._last_failure[name],
                    model=self._configured_models[name],
                    pricing_status="known" if name in self._pricing else "unknown",
                    tier=self._pricing[name].tier if name in self._pricing else None,
                    eligible=self._eligible(name),
                    paid_authorization_required=(
                        name in self._pricing
                        and self._pricing[name].tier == "paid"
                        and not self._paid_authorized()
                    ),
                    free_tier_quota_status=(
                        "unverified"
                        if name in self._pricing and self._pricing[name].tier == "free"
                        else "not_applicable"
                    ),
                )
                for name in _ORDER
            ),
        )

    @property
    def budget_ledger(self) -> LLMBudgetLedger:
        if self._budget_ledger is None:
            raise self._budget_error or LLMBudgetError("LLM budget ledger is unavailable")
        return self._budget_ledger

    def pricing(self, provider: LLMProviderName) -> ProviderPricing | None:
        return self._pricing.get(provider)

    def pricing_status(self) -> tuple[LLMProviderPricingStatus, ...]:
        return tuple(
            LLMProviderPricingStatus(
                provider=name,
                model=self._configured_models[name],
                configured=name in self._providers,
                pricing_status="known" if name in self._pricing else "unknown",
                tier=self._pricing[name].tier if name in self._pricing else None,
                health=self._health[name],
                eligible=self._eligible(name),
                paid_authorization_required=(
                    name in self._pricing
                    and self._pricing[name].tier == "paid"
                    and not self._paid_authorized()
                ),
                free_tier_quota_status=(
                    "unverified"
                    if name in self._pricing and self._pricing[name].tier == "free"
                    else "not_applicable"
                ),
            )
            for name in _ORDER
        )

    def authorize(
        self,
        provider: LLMProviderName,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> BudgetAuthorization:
        return self.budget_ledger.authorize(
            provider=provider,
            model=model,
            pricing=self.pricing(provider),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _auto_order(self) -> tuple[LLMProviderName, ...]:
        default = self.settings.default_provider
        ordered = (default,) + tuple(name for name in _ORDER if name != default)
        position = {name: index for index, name in enumerate(ordered)}

        configured = tuple(name for name in ordered if name in self._providers)
        free = tuple(
            name for name in configured
            if (pricing := self._pricing.get(name)) is not None and pricing.tier == "free"
        )
        pool = free or tuple(name for name in configured if self._eligible(name))

        def cost_order(name: LLMProviderName) -> tuple[int, Decimal, int]:
            pricing = self._pricing.get(name)
            health_rank = 0 if self._health[name] in ("unknown", "healthy") else 1
            if pricing is None or pricing.tier == "free":
                return health_rank, Decimal("0"), position[name]
            return (
                health_rank,
                pricing.input_inr_per_million + pricing.output_inr_per_million,
                position[name],
            )

        return tuple(
            sorted(
                pool,
                key=cost_order,
            )
        )

    def _eligible(self, name: LLMProviderName) -> bool:
        if name not in self._providers:
            return False
        pricing = self._pricing.get(name)
        if pricing is None:
            return False
        return pricing.tier == "free" or self._paid_authorized()

    def _paid_authorized(self) -> bool:
        return (
            self._budget_ledger is not None
            and self._budget_ledger.policy.paid_provider_authorized
        )

    def empty_selection_reason(self) -> LLMFallbackReason:
        if not self._providers:
            return "not_configured"
        if not any(name in self._pricing for name in self._providers):
            return "pricing_unavailable"
        return "paid_provider_not_authorized"


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


def _optional_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


@lru_cache(maxsize=1)
def get_llm_provider_registry() -> LLMProviderRegistry:
    return LLMProviderRegistry()
