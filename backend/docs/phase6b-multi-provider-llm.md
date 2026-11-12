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
- `GEMINI_API_KEY` and optional `GEMINI_MODEL` (default `gemini-3.8-flash`)
- `FRIDAY_LLM_PROVIDER_MODE=auto|manual`
- `FRIDAY_LLM_DEFAULT_PROVIDER=openai|anthropic|gemini`
- `FRIDAY_LLM_MANUAL_FALLBACK_ENABLED=true|false` (default `false`)
- `FRIDAY_LLM_TIMEOUT_SECONDS` (bounded to 1–60 seconds)
- `FRIDAY_LLM_MAX_ATTEMPTS` (default `2`, bounded to 1–3)
- `FRIDAY_LLM_RETRY_BASE_SECONDS` (default `0.25`, bounded to 0–5 seconds)
- `FRIDAY_LLM_MAX_RETRY_DELAY_SECONDS` (default `10`, bounded to 0–60 seconds)
- `FRIDAY_LLM_MAX_OUTPUT_TOKENS` (bounded to 256–4096)
- `FRIDAY_LLM_BUDGET_DB_PATH` (default `backend/runtime/llm_usage.sqlite3`)
- `FRIDAY_LLM_MONTHLY_BUDGET_INR` (default `500`)
- `FRIDAY_LLM_USABLE_BUDGET_INR` (default `450`)
- `FRIDAY_LLM_PROTECTED_RESERVE_INR` (default `50`)
- `FRIDAY_LLM_DAILY_LIMIT_INR` (default `25`)
- `FRIDAY_LLM_MAX_REQUEST_ESTIMATE_INR` (default `5`)
- `FRIDAY_LLM_PAID_PROVIDER_AUTHORIZED=true|false` (default `false`)
- `FRIDAY_LLM_INR_PER_USD` when configured pricing uses USD
- `FRIDAY_LLM_PROVIDER_PRICING_JSON`, an explicit provider pricing object with
  `tier`, `currency`, `input_per_million`, and `output_per_million`

Unknown or malformed pricing is ineligible and never becomes zero-cost. A provider
may be labelled `free` only by explicit configuration with zero input and output
rates. Paid dispatch requires the separate paid-provider authorization flag.

The provider catalogue at `GET /api/llm/providers` returns configuration and
health labels only. `available` becomes true only after a successful call; an
untested configured provider remains `unknown`. The endpoint never returns keys.
Request-level manual selection uses `llm_provider_mode`, `llm_provider`, and
`llm_model` on `POST /api/analyze`; a manual model must exactly match the
backend-configured catalogue label.

## Selection and fallback

Auto mode deterministically uses explicitly eligible free-tier providers when any
are configured. It does not silently cross from that free tier to a paid provider
after a failure. Only when no eligible free provider is configured can it select
the cheapest explicitly priced and explicitly authorized paid provider. The
configured default breaks ties. It may move to another provider in the same
eligible selection pool only
after retryable transport, timeout, rate-limit, or availability failures. Invalid
requests, malformed output, schema failures, unsafe narratives, and invalid
evidence references fail closed without blind cross-provider retries. Manual mode
does not switch providers unless its explicit fallback setting is enabled.

Provider HTTP failures preserve only bounded diagnostics: the status code, a
strictly validated provider reason code, and a redacted message of at most 240
characters. Raw response bodies, headers, request URLs, prompts, credentials, and
provider detail objects are never returned. These fields explain a deterministic
fallback but do not alter retry classification or authorize execution.

Transient transport, timeout, rate-limit, and provider-availability failures use
bounded adapter retries with exponential backoff and jitter. A valid `Retry-After`
value is honored only within the configured maximum delay; FRIDAY never waits past
that bound and never retries authentication, invalid-request, malformed-response,
schema, or evidence failures. No response headers or bodies are exposed.

Gemini 3 requests use `thinkingLevel=low` to reduce narrative-synthesis latency.
Older Gemini model overrides do not receive that field. JSON mode, output-token
bounds, local Pydantic validation, and evidence-reference validation remain
required for every Gemini response.

Provider health is process-local and passive: FRIDAY performs no separate health
probe. An unknown configured provider is eligible for its first request; successful
calls mark it healthy and failures record a sanitized runtime health status and
reason. Runtime health is diagnostic only: it never removes a configured provider
from a later automatic request. Each analysis starts from the deterministic provider
order, preferring `FRIDAY_LLM_DEFAULT_PROVIDER`, so a temporary failure does not
require a backend restart.

Free-tier quota status is reported as unverified unless independently configured;
FRIDAY does not infer quota availability from the presence of a key.

## Budget ledger and sanitized API

Every authorized attempt obtains an atomic SQLite reservation before network
dispatch. The reservation includes request ID, provider, model, conservative input
and maximum-output token estimates, INR cost, and timestamp. Successful provider
responses reconcile provider-reported token usage and actual estimated cost; when a
provider omits usage telemetry, actual token counts remain unknown and the reserved
cost estimate is retained. Failures release
the reservation without deleting its audit row. SQLite `BEGIN IMMEDIATE` transactions
prevent concurrent requests from independently spending the same remaining budget.

The monthly usable ceiling excludes the protected reserve. The daily and per-request
limits apply before paid dispatch. Process crashes intentionally leave reservations
pending and therefore fail closed rather than risk overspending; administrative
reconciliation is required for abandoned reservations.

`GET /api/llm/budget` returns aggregate limits and committed usage.
`GET /api/llm/usage?limit=100` returns bounded sanitized ledger rows. Neither endpoint
returns credentials, prompts, response bodies, or provider headers.

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
