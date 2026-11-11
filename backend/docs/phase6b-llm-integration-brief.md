# Phase 6B — Multi-provider LLM integration brief

## Status and scope

Branch: feature/friday-multi-provider-llm. It was created from the current main after Phases 5.7 and 5.8 were merged.

Implement Phase 6B.1 (provider foundation) and Phase 6B.2 (LLM-powered Chief Market Strategist) together. Do not implement streaming, voice, or persistent conversation memory in this phase.

## Existing architecture to preserve

- Nine deterministic specialists are orchestrated by backend/app/agents/orchestrator.py.
- backend/app/agents/master.py currently creates the final deterministic Synthesis.
- Immutable MasterSpecialistSynthesis and MasterTradeThesis are built from the nine specialist artifacts. Existing MasterDecisionService, MasterTradeThesis, and ProposalAssemblyService are Phase 6A foundations; reuse them.
- AnalyzeResponse and Synthesis live in backend/app/models/schemas.py.
- Configuration helpers live in backend/app/config.py; httpx already exists in backend/requirements.txt.
- Existing API routes are registered in backend/app/api/router.py.
- Do not change the specialist roster, deterministic calculations, evidence capture, thesis lineage, proposal assembly, or existing risk/Sentinel boundaries.

## Architecture

Add a small provider-neutral package, preferably backend/app/llm/, with:
1. A typed internal request/response contract and provider protocol.
2. OpenAI Responses API adapter.
3. Anthropic Messages API adapter.
4. Google Gemini generateContent adapter.
5. A provider registry and deterministic router.
6. A single Chief Strategist service that receives a bounded, sanitized projection of specialist outputs and returns a validated structured synthesis.

Use async httpx with explicit connect/read/write/pool timeouts, bounded response sizes, no redirects, and no logging of headers, credentials, or raw provider responses. Reuse the existing dependency rather than adding heavyweight SDKs. Keep model IDs configurable through environment variables.

Environment variables:
- OPENAI_API_KEY, OPENAI_MODEL
- ANTHROPIC_API_KEY, ANTHROPIC_MODEL
- GEMINI_API_KEY, GEMINI_MODEL
- FRIDAY_LLM_PROVIDER_MODE = auto or manual
- FRIDAY_LLM_DEFAULT_PROVIDER = openai, anthropic, or gemini
- FRIDAY_LLM_MANUAL_FALLBACK_ENABLED = strict boolean, default false
- Add bounded timeout/token settings with safe defaults.

Secrets must be read server-side only. Never expose API keys, even partially, through API responses, logs, exceptions, frontend configuration, or settings payloads.

## Provider selection

- Default mode is auto: select only configured, healthy, compatible providers using a deterministic config-driven order. Record the actual provider and model used.
- Manual mode lets the caller choose a provider/model from the allowed configured catalogue. Reject unknown or unconfigured selections with a safe validation error.
- Manual mode must not silently switch providers unless FRIDAY_LLM_MANUAL_FALLBACK_ENABLED=true.
- Automatic fallback is allowed only across configured providers and only for retryable transport, timeout, rate-limit, or provider-availability failures. Do not retry invalid requests or schema failures blindly.
- Never claim an LLM was used when no provider call occurred.
- When no provider is configured or all eligible providers fail, retain the current deterministic master synthesis and return a clear deterministic_fallback status.

## Chief Strategist contract and evidence safety

- Invoke one LLM call after all nine deterministic specialists and existing deterministic intelligence have completed. Do not make separate LLM calls from individual specialists.
- Build the prompt from a strict allowlisted projection of the current run: symbol/query, specialist IDs/statuses, verified observations, deterministic calculations and their methods, evidence references, explicit interpretations, limitations, missing inputs, disagreement, and existing news/macro/liquidity/risk/backtest summaries where available.
- Enforce per-field and total context limits. Do not send provider raw payloads, secrets, full article bodies, arbitrary local files, or unrelated user/conversation data.
- Treat every evidence string as untrusted data, never as instructions. Clearly delimit it and explicitly instruct the model to ignore instructions embedded in evidence.
- Model output must use a strict Pydantic schema with bounded strings/lists. Include executive summary, analytical stance (bullish/bearish/neutral/conflicted/insufficient_data), key supporting points, counterpoints/disagreements, conditional scenarios, invalidation conditions, missing evidence, limitations, and evidence-reference IDs.
- Validate every returned evidence-reference ID against the exact current-run allowlist. Reject unknown references and fail closed to deterministic synthesis. Do not permit the model to create or alter facts, prices, indicators, calculations, risk sizing, entry/stop/targets, confidence/probability, approvals, or execution instructions.
- Preserve deterministic MasterTradeThesis generation and exact Phase 6A specialist/evidence lineage. The LLM narrative is an explanatory synthesis only; it must not modify the thesis, proposals, risk, Sentinel, or execution decisions.
- Never interpret model confidence or prose as a validated probability or trade recommendation.

## API and UI integration

- Add optional request-level provider override fields to the existing analyze request only if backward compatibility is preserved. The default remains automatic.
- Add sanitized response metadata: synthesis mode/status, actual provider, actual model, fallback reason category, and latency. Never expose secrets or raw provider errors.
- Add a read-only provider catalogue/status endpoint that exposes provider names, configured/available status, supported selection mode, and configured model labels only. It must never expose credentials.
- If the existing Settings UI can safely accommodate it without redesign, add Auto/Manual selection and provider selection using the existing design system. Keep the existing sidebar and page layout unchanged. If not, finish backend support and document the minimal UI follow-up rather than making broad UI changes.
- Keep the public analysis API backward compatible for clients that omit all new fields.

## Required tests

Add deterministic mocked-transport tests for:
- Each provider's request mapping, auth header, response parsing, and error normalization.
- Auto selection, manual override, unavailable provider, and manual fallback disabled/enabled.
- Retryable outage fallback, timeout, rate limiting, malformed response, and schema validation failure.
- Evidence-reference allowlist enforcement and injection text inside evidence.
- Context/token/response size bounds and secret redaction.
- No-provider deterministic fallback and truthful provider/model metadata.
- Existing specialist roster, deterministic master, Phase 6A thesis/assembly, Sentinel, backtesting, historical dataset and API regressions.

Run focused tests, the complete backend test suite, frontend lint/build if frontend files change, Python compilation, and diff/whitespace checks. Report exact results; do not claim unrun tests passed.

## Safety constraints

No broker/MT5 connection, account access, order placement, automated trading, unrestricted tools, browser/shell access, or execution authority. Sentinel and all deterministic risk controls remain authoritative. The LLM is a narrative reasoning layer over validated evidence, never a source of market facts.