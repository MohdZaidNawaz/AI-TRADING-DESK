# Phase 6B — Multi-provider LLM strategist synthesis

## Implemented boundary

FRIDAY can optionally use OpenAI Responses, Anthropic Messages, or Google Gemini
GenerateContent for one evidence-bound Chief Market Strategist narrative after the
nine deterministic specialists have completed. The LLM layer is explanatory only.
It cannot change deterministic calculations, the Master Trade Thesis, proposals,
risk sizing, Sentinel decisions, approvals, submission, or execution.

If no provider is configured, a provider is unavailable, output is malformed, an
unknown evidence reference is returned, or narrative safety validation fails, the
existing deterministic synthesis is returned unchanged with a sanitized
`deterministic_fallback` status.

## Backend-only configuration

Set provider credentials only in the backend process environment:

- `OPENAI_API_KEY` and optional `OPENAI_MODEL`
- `ANTHROPIC_API_KEY` and optional `ANTHROPIC_MODEL`
- `GEMINI_API_KEY` and optional `GEMINI_MODEL`
- `FRIDAY_LLM_PROVIDER_MODE=auto|manual`
- `FRIDAY_LLM_DEFAULT_PROVIDER=openai|anthropic|gemini`
- `FRIDAY_LLM_MANUAL_FALLBACK_ENABLED=true|false` (default `false`)
- `FRIDAY_LLM_TIMEOUT_SECONDS` (bounded to 1–60 seconds)
- `FRIDAY_LLM_MAX_OUTPUT_TOKENS` (bounded to 256–4096)

The provider catalogue at `GET /api/llm/providers` returns configuration and
health labels only. `available` becomes true only after a successful call; an
untested configured provider remains `unknown`. The endpoint never returns keys.
Request-level manual selection uses `llm_provider_mode`, `llm_provider`, and
`llm_model` on `POST /api/analyze`; a manual model must exactly match the
backend-configured catalogue label.

## Selection and fallback

Auto mode uses a deterministic configured-provider order beginning with
`FRIDAY_LLM_DEFAULT_PROVIDER`. It may move to another configured provider only
after retryable transport, timeout, rate-limit, or availability failures. Invalid
requests, malformed output, schema failures, unsafe narratives, and invalid
evidence references fail closed without blind cross-provider retries. Manual mode
does not switch providers unless its explicit fallback setting is enabled.

Provider health is process-local and passive: FRIDAY performs no separate health
probe. An unknown configured provider is eligible for its first request; successful
calls mark it healthy and retryable failures mark it unavailable until process
restart.

## Evidence and output controls

Only a bounded allowlisted projection of the current deterministic run is sent.
Evidence is explicitly delimited as untrusted data. Raw provider payloads, article
bodies, files, credentials, headers, and unrelated conversation history are not
included. Responses have a byte limit and must validate against the strict
`ChiefStrategistNarrative` schema. Every returned evidence ID must belong to the
current run. Numeric claims and execution or recommendation directives are rejected.

The existing frontend remains compatible and can continue omitting all LLM fields.
A future narrow Settings UI may consume the provider catalogue and optional request
fields; no frontend redesign is part of Phase 6B.
