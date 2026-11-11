"""Minimal async HTTP adapters for supported LLM providers."""

import json
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


class _HTTPProvider:
    name: LLMProviderName

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
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

    async def generate(self, request: LLMProviderRequest) -> LLMProviderResult:
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
        except LLMProviderError:
            raise
        except httpx.TimeoutException as error:
            raise LLMProviderError(LLMFailureCategory.TIMEOUT, retryable=True) from error
        except httpx.RequestError as error:
            raise LLMProviderError(
                LLMFailureCategory.TRANSPORT_ERROR, retryable=True
            ) from error

        if status >= 400:
            raise _http_error(status)
        try:
            payload = json.loads(content)
            json_text = self._extract(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as error:
            raise LLMProviderError(
                LLMFailureCategory.MALFORMED_RESPONSE, retryable=False
            ) from error
        return LLMProviderResult(provider=self.name, model=self.model, json_text=json_text)

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _payload(self, request: LLMProviderRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _extract(self, payload: object) -> str:
        raise NotImplementedError


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
        return {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": request.user_prompt}]}
            ],
            "generationConfig": {
                # Use JSON mode without relying on provider-side schema
                # enforcement. The complete Pydantic contract and evidence
                # safety checks are enforced locally after parsing.
                "responseMimeType": "application/json",
                "maxOutputTokens": request.max_output_tokens,
            },
        }

    def _extract(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise TypeError
        parts = payload["candidates"][0]["content"]["parts"]
        text = parts[0]["text"]
        if not isinstance(text, str) or not text:
            raise ValueError
        return text


def _http_error(status: int) -> LLMProviderError:
    if status in (401, 403):
        return LLMProviderError(LLMFailureCategory.AUTHENTICATION, retryable=False)
    if status == 429:
        return LLMProviderError(LLMFailureCategory.RATE_LIMIT, retryable=True)
    if status in (408, 409) or status >= 500:
        return LLMProviderError(LLMFailureCategory.PROVIDER_UNAVAILABLE, retryable=True)
    return LLMProviderError(LLMFailureCategory.INVALID_REQUEST, retryable=False)
