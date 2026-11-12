"""Shared provider contracts and safe error normalization."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.models.llm_synthesis import LLMProviderName


class LLMFailureCategory(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TRANSPORT_ERROR = "transport_error"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "malformed_response"


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        category: LLMFailureCategory,
        *,
        retryable: bool,
        http_status_code: int | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(category.value)
        self.category = category
        self.retryable = retryable
        self.http_status_code = http_status_code
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class LLMProviderRequest:
    system_prompt: str
    user_prompt: str
    json_schema: dict[str, object]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMProviderResult:
    provider: LLMProviderName
    model: str
    json_text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reported_model: str | None = None


class LLMProvider(Protocol):
    name: LLMProviderName
    model: str

    async def generate(self, request: LLMProviderRequest) -> LLMProviderResult: ...
