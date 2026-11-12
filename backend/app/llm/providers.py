"""Minimal async HTTP adapters for supported LLM providers."""

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
import random
import re
import ssl
from typing import Any

import httpx

from app.llm.contracts import (
    LLMFailureCategory,
    LLMProviderError,
    LLMProviderRequest,
    LLMProviderResult,
)
from app.models.llm_synthesis import LLMProviderName

_MAX_RESPONSE_BYTES = 262_144
_MAX_PROVIDER_DIAGNOSTIC_CHARS = 240
_MAX_PROVIDER_CODE_CHARS = 64
_SAFE_PROVIDER_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_SENSITIVE_PARAMETER_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|token|secret|password)\b\s*[:=]\s*\S+"
)
_QUOTED_VALUE_PATTERN = re.compile(r"([\"'`])[^\"'`]{1,160}\1")
_TOKEN_LIKE_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")


class _HTTPProvider:
    name: LLMProviderName

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 2,
        retry_base_seconds: float = 0.25,
        max_retry_delay_seconds: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = httpx.Timeout(
            connect=timeout_seconds,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )
        self._transport = transport
        self._max_attempts = min(max(max_attempts, 1), 3)
        self._retry_base_seconds = min(max(retry_base_seconds, 0.0), 5.0)
        self._max_retry_delay_seconds = min(max(max_retry_delay_seconds, 0.0), 60.0)

    async def generate(self, request: LLMProviderRequest) -> LLMProviderResult:
        for attempt in range(self._max_attempts):
            try:
                return await self._generate_once(request)
            except LLMProviderError as error:
                if not error.retryable or attempt + 1 >= self._max_attempts:
                    raise
                delay = self._retry_delay(error, attempt)
                if delay is None:
                    raise
                if delay > 0:
                    await asyncio.sleep(delay)
        raise AssertionError("provider attempt loop exhausted")

    async def _generate_once(self, request: LLMProviderRequest) -> LLMProviderResult:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                verify=ssl.create_default_context(),
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    self._url(),
                    headers=self._headers(),
                    json=self._payload(request),
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_RESPONSE_BYTES:
                            raise LLMProviderError(
                                LLMFailureCategory.RESPONSE_TOO_LARGE,
                                retryable=False,
                            )
                    status = response.status_code
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        except LLMProviderError:
            raise
        except httpx.TimeoutException as error:
            raise LLMProviderError(LLMFailureCategory.TIMEOUT, retryable=True) from error
        except httpx.RequestError as error:
            raise LLMProviderError(
                LLMFailureCategory.TRANSPORT_ERROR, retryable=True
            ) from error

        if status >= 400:
            raise _http_error(
                status,
                content,
                forbidden_values=(
                    self._api_key,
                    request.system_prompt,
                    request.user_prompt,
                ),
                retry_after_seconds=retry_after,
            )
        try:
            payload = json.loads(content)
            json_text = self._extract(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as error:
            raise LLMProviderError(
                LLMFailureCategory.MALFORMED_RESPONSE, retryable=False
            ) from error
        input_tokens, output_tokens = self._usage(payload)
        return LLMProviderResult(
            provider=self.name,
            model=self.model,
            json_text=json_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _retry_delay(self, error: LLMProviderError, attempt: int) -> float | None:
        if error.retry_after_seconds is not None:
            if error.retry_after_seconds > self._max_retry_delay_seconds:
                return None
            return error.retry_after_seconds
        base = self._retry_base_seconds * (2**attempt)
        if base <= 0:
            return 0.0
        return min(base + random.uniform(0, base * 0.25), self._max_retry_delay_seconds)

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _payload(self, request: LLMProviderRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _extract(self, payload: object) -> str:
        raise NotImplementedError

    def _usage(self, payload: object) -> tuple[int | None, int | None]:
        del payload
        return None, None


class OpenAIProvider(_HTTPProvider):
    name: LLMProviderName = "openai"

    def _url(self) -> str:
        return "https://api.openai.com/v1/responses"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, request: LLMProviderRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "instructions": request.system_prompt,
            "input": request.user_prompt,
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "friday_chief_strategist",
                    "strict": True,
                    "schema": request.json_schema,
                }
            },
        }

    def _extract(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise TypeError
        for item in payload["output"]:
            if isinstance(item, dict) and item.get("type") == "message":
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            return text
        raise ValueError

    def _usage(self, payload: object) -> tuple[int | None, int | None]:
        if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
            return None, None
        usage = payload["usage"]
        return _token_count(usage.get("input_tokens")), _token_count(
            usage.get("output_tokens")
        )


class AnthropicProvider(_HTTPProvider):
    name: LLMProviderName = "anthropic"

    def _url(self) -> str:
        return "https://api.anthropic.com/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _payload(self, request: LLMProviderRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": request.json_schema,
                }
            },
        }

    def _extract(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise TypeError
        for part in payload["content"]:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    return text
        raise ValueError

    def _usage(self, payload: object) -> tuple[int | None, int | None]:
        if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
            return None, None
        usage = payload["usage"]
        return _token_count(usage.get("input_tokens")), _token_count(
            usage.get("output_tokens")
        )


class GeminiProvider(_HTTPProvider):
    name: LLMProviderName = "gemini"

    def _url(self) -> str:
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def _payload(self, request: LLMProviderRequest) -> dict[str, Any]:
        generation_config: dict[str, Any] = {
            # Use JSON mode without relying on provider-side schema
            # enforcement. The complete Pydantic contract and evidence
            # safety checks are enforced locally after parsing.
            "responseMimeType": "application/json",
            "maxOutputTokens": request.max_output_tokens,
        }
        if _is_gemini_3_model(self.model):
            generation_config["thinkingConfig"] = {"thinkingLevel": "low"}
        return {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": request.user_prompt}]}
            ],
            "generationConfig": generation_config,
        }

    def _extract(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise TypeError
        parts = payload["candidates"][0]["content"]["parts"]
        text = parts[0]["text"]
        if not isinstance(text, str) or not text:
            raise ValueError
        return text

    def _usage(self, payload: object) -> tuple[int | None, int | None]:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("usageMetadata"), dict
        ):
            return None, None
        usage = payload["usageMetadata"]
        return _token_count(usage.get("promptTokenCount")), _token_count(
            usage.get("candidatesTokenCount")
        )


def _is_gemini_3_model(model: str) -> bool:
    normalized = model.strip().casefold()
    return normalized.startswith(("gemini-3.", "gemini-3-"))


def _token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _http_error(
    status: int,
    content: bytes | bytearray = b"",
    *,
    forbidden_values: tuple[str, ...] = (),
    retry_after_seconds: float | None = None,
) -> LLMProviderError:
    provider_error_code, provider_error_message = _safe_provider_diagnostic(
        content,
        forbidden_values=forbidden_values,
    )
    if status in (401, 403):
        category = LLMFailureCategory.AUTHENTICATION
        retryable = False
    elif status == 429:
        category = LLMFailureCategory.RATE_LIMIT
        retryable = True
    elif status in (408, 409) or status >= 500:
        category = LLMFailureCategory.PROVIDER_UNAVAILABLE
        retryable = True
    else:
        category = LLMFailureCategory.INVALID_REQUEST
        retryable = False
    return LLMProviderError(
        category,
        retryable=retryable,
        http_status_code=status,
        provider_error_code=provider_error_code,
        provider_error_message=provider_error_message,
        retry_after_seconds=retry_after_seconds if status == 429 else None,
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            return None
        seconds = (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if seconds < 0 or seconds > 3600:
        return None
    return seconds


def _safe_provider_diagnostic(
    content: bytes | bytearray,
    *,
    forbidden_values: tuple[str, ...],
) -> tuple[str | None, str | None]:
    """Extract a small safe diagnostic without retaining the provider body."""

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None

    raw_code = error.get("status") or error.get("code") or error.get("type")
    code = _bounded_provider_code(raw_code)
    message = _sanitize_provider_message(
        error.get("message"),
        forbidden_values=forbidden_values,
    )
    return code, message


def _bounded_provider_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()[:_MAX_PROVIDER_CODE_CHARS]
    if not _SAFE_PROVIDER_CODE.fullmatch(normalized):
        return "UNCLASSIFIED_PROVIDER_ERROR"
    return normalized


def _sanitize_provider_message(
    value: object,
    *,
    forbidden_values: tuple[str, ...],
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    for forbidden in forbidden_values:
        if forbidden:
            normalized = normalized.replace(forbidden, "[REDACTED]")
    normalized = _URL_PATTERN.sub("[REDACTED_URL]", normalized)
    normalized = _SENSITIVE_PARAMETER_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        normalized,
    )
    normalized = _QUOTED_VALUE_PATTERN.sub("[REDACTED]", normalized)
    normalized = _TOKEN_LIKE_PATTERN.sub("[REDACTED]", normalized)
    normalized = "".join(
        character if 32 <= ord(character) <= 126 else " "
        for character in normalized
    )
    normalized = " ".join(normalized.split())
    return normalized[:_MAX_PROVIDER_DIAGNOSTIC_CHARS] or None
