# FRIDAY desk frontend

React, TypeScript, and Vite interface for the local FRIDAY research desk.

## Run

Start the backend first on http://127.0.0.1:8000, then:

~~~powershell
cd frontend
npm install
npm run dev
~~~

The API base defaults to http://127.0.0.1:8000. For another local address,
set VITE_API_BASE_URL before starting Vite.

## What the interface represents

- The roster is the exact nine-specialist architecture; FRIDAY Master is
  separate.
- The command bar calls the deterministic POST /api/analyze workflow.
- Market cards are mock reference fixtures and remain visibly labeled as mock
  whether they come from the API or the local fallback.
- The header and session panel distinguish HTTP availability from production
  readiness.
- The readiness report cannot authorize execution. The interface exposes no
  execution control.

No live market feed, AI provider, broker connection, MT5 session, or order
submission is implied by this UI.

## Validate

~~~powershell
npm run lint
npm run build
~~~

There is currently no frontend test runner configured; the production build
performs the TypeScript check.
