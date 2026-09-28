# FRIDAY Core API

FastAPI service for the Nimbus Desk frontend and its deterministic agent engine. All analysis and market data is currently mocked; this service does not connect to AI providers, brokers, MT5, trading platforms, or external data providers.

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

Example analysis request:

```json
{
  "symbol": "XAUUSD",
  "query": "Review technical momentum and risk"
}
```

The master orchestrator uses deterministic keyword routing. It returns a result for all eight agents, marking agents that were not selected as `skipped`, followed by a mock synthesized response. The architecture under `app/agents` is intentionally isolated so model-backed agents and real data adapters can be introduced later.

## Multi-source ingestion architecture

The backend keeps external-source details outside FRIDAY Core:

```text
source connector -> normalized event -> ingestion service -> FRIDAY Core -> agents -> master synthesis
```

- `app/connectors/` contains source-specific adapters.
- `app/models/events.py` defines provider-neutral source metadata, data domains, asset classes, event types, and normalized event fields.
- `app/ingestion/` defines the `SourceConnector` contract and the provider-agnostic ingestion hand-off.
- `app/agents/` receives only normalized events; it does not parse provider payloads.

TradingView is currently the only connector, classified as a webhook market-data source. Future market-data, financial-news, economic-calendar, fundamental, forex, crypto, commodities, sentiment, or alternative-data integrations should implement `SourceConnector`, normalize their payload into `NormalizedDataEvent`, and send it through `IngestionService`. No external provider is connected by this foundation.

## TradingView webhooks

`POST /api/webhooks/tradingview/{webhook_token}` receives TradingView alert JSON, validates it, normalizes it into an internal market event, and acknowledges it immediately with HTTP 200. FRIDAY Core processes the normalized event in a background task through the existing deterministic analysis flow; the endpoint never executes trades or connects to brokers, MT5, or market-data providers.

### Authentication

Generate a long random token locally and set it as `TRADINGVIEW_WEBHOOK_SECRET` before starting the API. Configure the same token as the final path segment of the TradingView webhook URL. The API compares the URL token with a constant-time check, never includes it in a response, and never reads credentials from the JSON payload.

The API fails closed with HTTP 503 when this environment variable is unset or blank, and returns HTTP 401 for an incorrect URL token. Store a unique, high-entropy value in your operating-system or deployment secret manager; never commit it, add it to source files, or use a real secret in tests.

Treat the full webhook URL as a secret: use HTTPS, do not share it publicly, and rotate the token immediately if it is exposed. The application does not log the token. Configure any reverse proxy or observability system to redact `/api/webhooks/tradingview/*`; the documented Uvicorn command disables access logs so it does not record request paths.

### Alert message template

Configure TradingView with a JSON message such as the following. TradingView resolves the placeholders before sending the alert:

```json
{
  "symbol": "{{exchange}}:{{ticker}}",
  "timeframe": "{{interval}}",
  "event": "price_alert",
  "price": "{{close}}",
  "time": "{{time}}",
  "open": "{{open}}",
  "high": "{{high}}",
  "low": "{{low}}",
  "close": "{{close}}",
  "volume": "{{volume}}"
}
```

The normalizer separates an `EXCHANGE:SYMBOL` value, converts numeric strings to numbers, converts ISO-8601 or Unix timestamps to UTC, and retains the original JSON as `raw_payload` for future agent adapters. A resolved `symbol` or `ticker` is required; all price fields are optional.

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
