# FRIDAY Release-Readiness Remediation

Date: 2026-10-20

Baseline: `73fa2d590b05ecb9e64d2b4cdd1a426353e32b30`

This report describes the repository and controls that can be verified locally.
It is not a production authorization, broker attestation, or evidence that an
external authority exists.

## Status vocabulary

- **Available**: implemented and usable for its stated local, non-authorizing
  purpose.
- **Degraded**: useful locally, but a named source or assurance is absent.
- **Unavailable**: the required implementation or configured dependency is not
  present.
- **Blocked**: deliberately prevented by a safety boundary or missing external
  authorization.

## Release-readiness matrix

| Capability | Responsible modules | State | Verified implementation and assumptions | External dependency or remaining gap | Condition for next state |
| --- | --- | --- | --- | --- | --- |
| Frontend/backend integration | `frontend/src/services/api.ts`, `app/main.py`, `app/api/routes/*` | Available for local research | Typed health, readiness, roster, mock-market, and deterministic-analysis calls; browser timeouts; no execution client | No browser E2E harness; non-loopback deployment is unsupported | Add browser tests; design authenticated ingress before any remote exposure |
| Specialist orchestration | `app/agents/*`, `app/services/friday_core.py` | Available for deterministic research | Exact nine-specialist roster, immutable evidence snapshot per run, typed outputs, conflict-preserving Master synthesis, Pilot disabled | No configured production AI provider | A reviewed provider adapter with bounded time, size, token, provenance, and tool privileges |
| Market display | `app/data/mock_data.py`, `app/models/schemas.py`, `/api/markets` | Available as mock only | Response is literally `data_source=mock`; each market is literally mock, finite, positive, and range coherent | No live dashboard feed | Add a separately authenticated, provenance-carrying source without changing mock semantics |
| Provider connectors | `app/connectors/*`, `app/services/*data*` | Degraded | Provider-neutral normalization, bounded HTTP timeouts, input validation, credential sanitization, generic errors | Runtime availability and credentials are not probed; providers are not production authority | Configure and operationally review each read-only provider independently |
| Historical series and derived features | `app/models/market_series.py`, `app/services/market_series.py`, `app/services/derived_features.py` | Available for supplied evidence | Immutable ordered bars, source isolation, gap/provisional handling, deterministic methods and fingerprints | No automatic historical provider ingestion | Supply authenticated or cached series with explicit freshness and provenance |
| Personal key protection | `app/services/windows_dpapi_key_provider.py` | Available for local Windows custody | Current-user DPAPI, exact ACL checks, lifecycle lock, fail-closed corruption/recovery, explicit rotation/revocation | Exportable software key; local state is not independent governance or rollback protection | Establish separately governed trust and recovery procedures; hardware custody would be a separate design |
| Personal trust bootstrap | `app/models/personal_trust_bootstrap.py`, `app/services/personal_trust_bootstrap.py` | Degraded | Exact key/generation/scope/anchor/ceremony and chronology binding; recovery remains diagnostic | Human confirmation is modeled, not independently evidenced | Complete an out-of-band ceremony governed outside the subject key and retain independent records |
| Independent trust anchor | `app/models/independent_trust_verification.py`, `app/services/independent_trust_verification.py` | Unavailable operationally | A separately supplied Ed25519 key can verify canonical statements; a response cannot supply its verifier; configuration stays non-authorizing | No independently provisioned and governed anchor source, rotation, suspension, or revocation service | Provision and review an anchor through an authority-independent operational channel |
| Authenticated authority and key status | same as above | Unavailable operationally | Authority, anchor, subject, generation, scope, challenge, request, time, state, and sequence are signature-bound; terminal states fail closed | No authenticated external status/revocation/recovery authority | Integrate a real independently trusted read-only authority and verify outage/recovery behavior |
| Replay and rollback protection | independent-trust replay protocol | Unavailable | Exact replay binding and fail-closed unavailable default | No external atomic monotonic store; local dictionaries and SQLite are not represented as external protection | Deploy an independently protected atomic service with concurrency, restart, and rollback guarantees |
| Broker execution evidence | `app/models/trusted_execution_evidence.py`, `app/services/trusted_execution_evidence.py` | Degraded | Typed read-only account/instrument/quote/position/order/risk lineage, retrieval chronology, freshness, contradiction checks | Source remains explicitly unauthenticated; no broker call was made | Integrate independently authenticated read-only evidence with durable provenance and replay protection |
| Account and instrument binding | evidence models and MT5 contracts | Available for supplied snapshots | Exact source account, server/reference, canonical and broker symbol, volume, order, filling, quote, and policy lineage validation | Facts are only as authoritative as their absent trusted source | Bind an authenticated source attestation to the exact request and evidence fingerprint |
| Broker reconciliation | reconciliation contracts and readiness gate | Unavailable authoritatively | Unknown or uncertain states fail closed and require reconciliation; no retry is inferred | No authoritative broker-state reconciliation provider | Add authenticated read-only reconciliation covering orders, deals, positions, and restart ambiguity |
| Policy and Sentinel risk | policy catalogs, governance, risk evidence, Sentinel gate | Degraded | Versioned policy, approval/revocation lifecycle, exact proposal/evidence lineage, fail-closed Sentinel-only gate | Independent approver and authoritative revocation source are absent | Establish independent policy governance and trusted evidence before production review |
| Production readiness | `app/models/production_authorization.py`, `app/services/production_authorization_readiness.py` | Blocked | Complete deterministic report; status and all authorization/submission/execution flags are literal false; caller input cannot grant authority | All external trust, broker, reconciliation, and review dependencies below | Satisfy every prerequisite independently; the diagnostic itself can never authorize |
| Execution boundary | Sentinel, reservations, `FinalExecutionGate`, Pilot, adapters | Blocked | Layered lineage, expiry, kill switch, idempotency, audit, reservation, and reconciliation checks; no public execution route | Explicit production authorization and reviewed submission boundary are absent | Separate security review plus explicit human authorization after all evidence dependencies exist |
| API/network security | `app/main.py`, API models and routes | Available for direct local use only | Direct peer must be loopback; unexpected browser origins fail; CORS credentials disabled; 1 MiB streamed body limit; bounded identifiers and generic provider errors | No user authentication, reverse-proxy trust model, or rate limiter | Keep loopback-only, or design authenticated ingress and rate limits before proxy or LAN exposure |
| Secret handling | configuration helpers, connector sanitizers, `app/observability/events.py` | Available with documented limits | `.env` and runtime state ignored; structured fields reject sensitive names, quoted or nested assignments, bearer/basic credentials, and private-key markers | Pattern filtering cannot identify arbitrary unlabeled secrets | Keep secrets out of log APIs; add an operational secret scanner and log-retention policy |
| Persistence and authority state | SQLite authority, audit, and reservation stores | Degraded | Explicit schemas, transactions, constraints, outbox, restart recovery, conflict detection, fail-closed corruption | Local databases are not tamper-proof and separate stores or external systems are not one atomic transaction | Document backup/restore and add protected external coordination only when operationally justified |
| Auditability and observability | audit stores, structured events, correlation IDs | Degraded | Durable local audit where configured; stable IDs and sanitized structured metadata | Readiness default has no durable audit service; local audit is not external evidence | Configure protected persistence and independent retention/review procedures |
| Recovery | DPAPI/key, durable authority, reservation recovery | Degraded | Missing, corrupt, or uncertain states fail closed; no silent key replacement or automatic order retry | No external rollback detection or authoritative broker reconciliation | Exercise documented backup/recovery with independent sequence and broker truth sources |

## Remediation completed in this mission

### Local API boundary

`app/main.py` rejects direct non-loopback peers before routing. Requests with an
`Origin` header are accepted only from the two local Vite origins. CORS
credentials are disabled because the application has no cookie or session
authentication. Declared and streamed request bodies are bounded to 1 MiB.

This control is intentionally narrow. A same-host reverse proxy would appear as
a loopback peer, so it must not be introduced without authenticated ingress,
trusted-proxy configuration, rate limits, and a separate review.

### Structured-log defense in depth

Structured fields reject credential assignments when keys are quoted inside
nested string renderings, authorization-scheme values, and private-key PEM
markers. This does not turn regex matching into a secret-detection service.
Callers remain responsible for never passing secrets to logging code.

### Truthful mock-market contract

Public fixture values must be finite and positive, the displayed price must be
inside its high/low range, every item is literally marked mock, and response
counts must match the returned list. These checks prevent malformed fixtures
from being rendered as coherent market data; they do not create live prices.

## Trust and authority model

The configured Ed25519 public-key contract proves only cryptographic validity
under that key. It does not prove who provisioned the key. An acceptable
operational anchor source must be established independently of the authority it
verifies and must bind authority ID, anchor ID, version, fingerprint, permitted
scope, validity, suspension, revocation, supersession, and recovery governance.
Replacement cannot be authorized only by the key or authority being replaced.

The existing verifier and unavailable replay implementation are retained rather
than duplicated. Until an external anchor source and atomic replay service are
genuinely configured, independent authority checks remain unavailable and
production readiness remains blocked.

## Broker evidence and reconciliation

Current broker contracts are read-only. Structural completeness, timestamps,
and lineage do not imply provider authentication. A future source must
authenticate independently, bind its attestation to the request and evidence
fingerprint, and provide an authoritative reconciliation view after ambiguous
submission outcomes or restart. No MT5 initialization, account query, or broker
request is part of this milestone.

## Operational Windows workflow

Backend (loopback only):

~~~powershell
cd C:\Users\mohdz\NimbusDesk\backend
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
~~~

Frontend:

~~~powershell
cd C:\Users\mohdz\NimbusDesk\frontend
npm run dev
~~~

Safe diagnostics:

~~~powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/readiness
~~~

Validation:

~~~powershell
cd C:\Users\mohdz\NimbusDesk\backend
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m compileall -q app tests

cd ..\frontend
npm run lint
npm run build
~~~

Health proves process liveness only. Readiness is a non-authorizing list of
blockers. Neither endpoint contacts providers, MT5, an account, or a broker.

## Validation evidence

The following checks completed against the final working tree without provider,
MT5, account, or broker access:

- Focused API, logging, orchestration, provider-mock, independent-trust,
  execution-evidence, and readiness tests: **131 passed**.
- Complete backend suite: **1036 passed**, zero failures.
- `python -m compileall -q app tests`: passed.
- `npm run lint`: passed.
- `npm run build`: passed; TypeScript compiled and Vite transformed 25 modules.
- `git diff --check`: passed after the complete patch review.

## Remaining release blockers

1. Independently governed trust-anchor provisioning, rotation, revocation, and
   recovery.
2. Authenticated external authority status and revocation evidence.
3. Externally durable atomic replay/rollback protection.
4. Authenticated broker, account, instrument, and position evidence.
5. Authoritative broker reconciliation for uncertain outcomes and restarts.
6. Authentication, trusted-proxy rules, and rate limits for any non-loopback
   deployment.
7. Independent production submission-boundary security review and explicit
   human authorization.

Production authorization, demo or live submission, and execution remain
disabled until all applicable blockers are independently satisfied and reviewed.
