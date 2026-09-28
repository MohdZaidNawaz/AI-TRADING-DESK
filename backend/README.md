# Nimbus Desk API

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
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API is available at `http://127.0.0.1:8000`, with interactive documentation at `http://127.0.0.1:8000/docs`.

## Endpoints

- `GET /api/health`
- `GET /api/agents`
- `GET /api/markets`
- `POST /api/analyze`

Example analysis request:

```json
{
  "symbol": "XAUUSD",
  "query": "Review technical momentum and risk"
}
```

The master orchestrator uses deterministic keyword routing. It returns a result for all eight agents, marking agents that were not selected as `skipped`, followed by a mock synthesized response. The architecture under `app/agents` is intentionally isolated so model-backed agents and real data adapters can be introduced later.
