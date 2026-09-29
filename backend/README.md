# FRIDAY Core API

FastAPI service for the Nimbus Desk frontend and its deterministic agent engine. FRIDAY can ingest TradingView webhook events and manually requested Twelve Data market snapshots. It does not connect to brokers, MT5, order execution, automated trading, or AI providers.

## Setup

From the `backend` directory on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

For development and tests:

```powershell
python -m pip install -r requirements-dev.txt
```

## Run

```powershell
python -m uvicorn app.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

The API is available at `http://127.0.0.1:8000`, with interactive documentation at `http://127.0.0.1:8000/docs`.

## Endpoints

- `GET /api/health`
- `GET /api/agents`
- `GET /api/markets`
- `POST /api/analyze`
- `POST /api/webhooks/tradingview/{webhook_token}`
- `POST /api/market-data/twelve-data/snapshot?symbol=XAU%2FUSD`

Example analysis request:

```json
{
  "symbol": "XAUUSD",
  "query": "Review technical momentum and risk"
}
```

The master orchestrator uses deterministic keyword routing. It returns a result for all nine specialists, marking agents that were not selected as `skipped`, followed by a mock synthesized response. The architecture under `app/agents` is intentionally isolated so model-backed agents and real data adapters can be introduced later.

## Multi-source ingestion architecture

The backend keeps external-source details outside FRIDAY Core:

```text
source connector -> normalized event -> ingestion service -> Evidence Store -> FRIDAY Core -> agents -> master synthesis
```

- `app/connectors/` contains source-specific adapters.
- `app/models/events.py` defines provider-neutral source metadata, data domains, asset classes, event types, and normalized event fields.
- `app/ingestion/` defines the `SourceConnector` contract and the provider-agnostic ingestion hand-off.
- `app/agents/` receives provider-neutral structured evidence; it does not parse provider payloads.

TradingView is a webhook market-data connector. Twelve Data is an API market-data connector using the same provider-neutral flow. Future market-data, economic-calendar, fundamental, forex, crypto, commodities, sentiment, or alternative-data integrations should implement `SourceConnector`, normalize their payload into `NormalizedDataEvent`, and send it through `IngestionService`. Financial-news and research connectors use `NewsResearchConnector` and `NormalizedNewsResearchEvent` instead.

## Phase 2 structured evidence architecture

FRIDAY converts validated normalized market and news events into a safe, immutable evidence projection before specialists consume it:

```text
NormalizedDataEvent / NormalizedNewsResearchEvent
    -> bounded process-local EvidenceStore
    -> immutable EvidenceSnapshot
    -> provider-neutral EvidenceRouter
    -> specialist-specific AgentEvidence
    -> AnalysisContext
```

`app/evidence/` contains the provider-neutral evidence projections, thread-safe bounded store, generic symbol matching, and nine-specialist routing policy. The store is process-local and non-persistent, so it resets when the API process restarts. It retains only bounded market/news evidence and creates deterministic snapshots without exposing mutable internal collections.

Evidence projections retain safe normalized fields and provenance only. They deliberately exclude provider `raw_payload` and provider-only attributes from market evidence, and exclude full article bodies from news evidence. The original normalized event remains unchanged for compatible API responses and transport behavior.

All nine specialists receive only their declared evidence subset. News is shared evidence, not a tenth specialist seat. Pilot Execution can receive read-only context but remains disabled: this architecture has no execution authority, broker integration, MT5 connection, credentials, order placement, or automated trading.

## Phase 3 trade proposal and Sentinel risk gate

Phase 3 adds internal, provider-neutral contracts for a non-executable `TradeProposal` and a `SentinelDecision`. A proposal references only safe evidence provenance, specialist-result digests, and optional observed normalized market facts. It does not contain raw provider payloads, article bodies, credentials, or executable instructions.

```text
immutable EvidenceSnapshot
    -> nine-specialist analysis
    -> F.R.I.D.A.Y. non-executable draft proposal
    -> Sentinel risk decision
    -> APPROVED / REJECTED / INSUFFICIENT_DATA
    -> future Pilot hand-off boundary (disabled)
```

The existing `/api/analyze` contract is unchanged. Phase 3 has no proposal or execution endpoint. FRIDAY's current deterministic agents do not produce structured direction, entry, stop, target, sizing, portfolio, or risk-limit data, so ordinary runs remain non-executable and Sentinel returns `INSUFFICIENT_DATA` rather than inventing those values.

An approval can exist only when explicit proposal parameters, fresh market evidence under an explicitly supplied policy, explicit portfolio/sizing context, structured specialist agreement, and any required news-risk assessment are all available. Invalid trade mathematics or configured risk-limit violations are `REJECTED`; absent or stale information is `INSUFFICIENT_DATA`.

Approved decisions are registered only in a lock-protected, process-local approval registry. Each approval is bound to the proposal ID and fingerprint, analysis ID, evidence fingerprint, evaluation time, and expiry. Re-evaluation is idempotent, and a Pilot hand-off claim is atomic and single-use. Restarting the backend clears this registry and invalidates every approval.

Pilot remains disabled in Phase 3. It verifies a matching registry-issued Sentinel approval and then returns `DISABLED_NO_EXECUTION`; it has no adapter, no execution API route, no MT5 import, no broker connection, no credentials, and no order placement capability.

## News and research foundation

The backend includes a provider-neutral staging layer for future financial-news and research sources:

```text
future provider connector -> NormalizedNewsResearchEvent -> NewsResearchIngestionService -> EvidenceStore
```

`NormalizedNewsResearchEvent` records stable source metadata, provider, title, HTTP(S) URL, timezone-aware timestamp, optional symbols and topics, and either a short summary or content value. `NewsResearchConnector` defines the future provider-adapter boundary, ensuring provider-specific parsing remains outside FRIDAY Core. `NewsResearchIngestionService` keeps a bounded, process-local collection of validated events.

This foundation does not register an API route, scrape or poll websites, call an external provider, or require credentials. `NewsResearchIngestionService` can hand validated provider-neutral events to the same process-local Evidence Store used by FRIDAY Core; it does not alter Alpha Vantage retrieval behavior or create background polling. A future provider integration must normalize its payload at the connector boundary before ingestion.

## Twelve Data market snapshots

Set `TWELVE_DATA_API_KEY` in the backend process environment before starting the API. The process environment takes precedence; for local development only, the backend also reads this single setting from `backend/.env` when the process variable is absent. Keep the value in your operating-system or deployment secret manager; never commit it or place it in a URL, request body, test, or documentation.

For local PowerShell use, set the environment variable in the same terminal that starts Uvicorn:

```powershell
$env:TWELVE_DATA_API_KEY = Read-Host "Twelve Data API key"
python -m uvicorn app.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

Request the initial supported test instrument, using Twelve Data's `XAU/USD` symbol format:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/market-data/twelve-data/snapshot?symbol=XAU%2FUSD"
```

The backend calls Twelve Data's quote endpoint, authenticates in the outbound request header, normalizes the response into `NormalizedDataEvent`, and sends it through the shared `IngestionService` into FRIDAY Core. The API response contains the normalized market event, never the API key. Provider failures return a generic error without provider credentials or response details. This manual route performs one upstream request and is intended for development and controlled testing; it is not a live stream or trading endpoint.

## TradingView webhooks

`POST /api/webhooks/tradingview/{webhook_token}` receives TradingView alert JSON, validates it, normalizes it into an internal market event, and acknowledges it immediately with HTTP 200. FRIDAY Core processes the normalized event in a background task through the existing deterministic analysis flow; the endpoint never executes trades or connects to brokers, MT5, or market-data providers.

### Authentication

Generate a long random token locally and set it as `TRADINGVIEW_WEBHOOK_SECRET` before starting the API. Configure the same token as the final path segment of the TradingView webhook URL. The API compares the URL token with a constant-time check, never includes it in a response, and never reads credentials from the JSON payload.

The API fails closed with HTTP 503 when this environment variable is unset or blank, and returns HTTP 401 for an incorrect URL token. Store a unique, high-entropy value in your operating-system or deployment secret manager; never commit it, add it to source files, or use a real secret in tests.

Treat the full webhook URL as a secret: use HTTPS, do not share it publicly, and rotate the token immediately if it is exposed. The application does not log the token. Configure any reverse proxy or observability system to redact `/api/webhooks/tradingview/*`; the documented Uvicorn command disables access logs so it does not record request paths.

### Alert message template

The connector supports a resolved `symbol` or `ticker`, `exchange`, `timeframe`/`interval`, `event`/`action`, `alert_name`, `message`, OHLCV fields, `price`, and an ISO-8601 or Unix `time`/`timestamp`. All price and volume fields are optional except the resolved symbol/ticker.

Configure TradingView with a JSON message such as the following. TradingView resolves the placeholders before sending the alert:

```json
{
  "symbol": "{{exchange}}:{{ticker}}",
  "timeframe": "{{interval}}",
  "event": "price_alert",
  "alert_name": "FRIDAY market observation",
  "message": "Optional context defined in the TradingView alert.",
  "price": "{{close}}",
  "time": "{{time}}",
  "open": "{{open}}",
  "high": "{{high}}",
  "low": "{{low}}",
  "close": "{{close}}",
  "volume": "{{volume}}",
  "indicators": {
    "custom_plot_one": "{{plot_0}}",
    "custom_plot_two": "{{plot_1}}"
  }
}
```

Indicator and plot values are optional. Send any object of unique string keys with finite numeric values in `indicators`, `plots`, or `indicator_values`; all are normalized into the provider-neutral `indicator_values` map. The connector does not reserve or interpret indicator names, so Pine alerts can use any plots or calculations without requiring backend changes.

The normalizer separates an `EXCHANGE:SYMBOL` value, converts numeric strings to numbers, converts ISO-8601 or Unix timestamps to UTC, preserves optional alert text and indicator values, and retains the original JSON as `raw_payload` for future agent adapters. Never include the webhook token or any secret in this JSON body.

### Local test

In one PowerShell window:

```powershell
$tokenBytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($tokenBytes)
$env:TRADINGVIEW_WEBHOOK_SECRET = [Convert]::ToHexString($tokenBytes).ToLowerInvariant()
python -m uvicorn app.main:app --no-access-log --host 127.0.0.1 --port 8000
```

In another PowerShell window:

```powershell
$body = @{ symbol = "OANDA:XAUUSD"; event = "price_alert"; price = "2648.32"; time = "2026-09-29T12:00:00Z" } | ConvertTo-Json
$webhookUrl = "http://127.0.0.1:8000/api/webhooks/tradingview/$($env:TRADINGVIEW_WEBHOOK_SECRET)"
Invoke-RestMethod -Method Post -Uri $webhookUrl -ContentType "application/json" -Body $body
```
