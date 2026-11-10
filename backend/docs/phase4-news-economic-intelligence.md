# Phase 4: News and Economic Intelligence

Phase 4 adds a non-authorizing, evidence-based news and macro layer to the
existing research pipeline. It does not add an AI model, broker integration, or
execution capability.

## Implemented

- GDELT DOC 2.0 is the primary, credential-free news-discovery provider. Each
  supported XAU/USD analysis runs five separate bounded article-list searches
  for Gold, monetary policy, US inflation, the US dollar/Treasury yields, and
  Gold-relevant geopolitical developments. Requests are sequentially paced,
  capped at 50 records, limited to one week, and are never retried.
- Alpha Vantage remains an explicitly optional integration for accounts with
  the required entitlement. It is not constructed or called automatically and
  is reported as `not_requested` when GDELT is the primary provider.
- A provider response is completely normalized before any item is staged.
  Malformed batches, timeouts, provider errors, and rate limits fail closed with
  bounded diagnostics. Relevant items are deduplicated and carry their original
  headline, publisher URL, publication time, retrieval time, source identity,
  provider identity, and freshness classification.
- Provider failures are classified as `network`, `authentication`,
  `invalid_request`, `rate_limit`, `timeout`, `server_error`,
  `malformed_response`, or the bounded `provider_error` fallback. Diagnostics
  may include a safe HTTP status/class, fixed reason code, failure stage, and
  request-sent state. Raw response bodies, request URLs, headers, credentials,
  and tracebacks are never exposed through the analysis response.
- GDELT `seendate` is retained as `seen_at`, not publication time. Original
  publication time remains null because DOC 2.0 article-list metadata does not
  establish it. Headline, URL, publisher domain, language, source country,
  search query, retrieval time, and source identity are retained; summaries,
  sentiment, article content, and causation are not invented.
- The deterministic macro layer separates the verified article or calendar fact
  from interpretation, a conditional Gold implication, and explicit uncertainty.
  It does not state that news caused a price move.
- Existing normalized economic-event evidence is projected without changing its
  lifecycle contract. Previous, forecast, and actual values remain distinct.
  Medium/high-impact scheduled events are described as potential volatility
  risk with unknown direction.
- Scout, Atlas, Sentinel, and the Master consume the new report when supported
  evidence exists. The public compatibility mode remains `deterministic_mock` to
  avoid implying that an LLM performed the analysis.

## Not operational

No external economic-calendar provider is currently configured. When no
validated calendar evidence has already been staged, the API reports
`provider_unavailable`; it does not invent releases, times, forecasts, or actual
values. Session analysis is limited to evidence timestamps and does not claim a
market-session feed.

GDELT is a discovery index, not an independent verifier of article content or
publisher claims. Its seen time describes when GDELT observed metadata. A
successful query does not prove completeness, original publication time,
article accuracy, market impact, causation, or direction. If any bounded GDELT
request or record validation fails, the batch is unavailable and FRIDAY does
not silently switch to Alpha Vantage.

News retrieval still requires the existing Alpha Vantage configuration and live
provider verification is separate from this offline implementation. A relevant
headline is contextual evidence, not proof of causation, direction, current
price, liquidity, positioning, or order flow.

All authority fields in the Phase 4 report are literal `false`. Production
authorization, submission, and execution remain disabled.
