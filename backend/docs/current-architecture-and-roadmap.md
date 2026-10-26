# Current architecture and remaining roadmap

This document describes the repository as implemented. It is not evidence of
production authorization.

## Runtime map

~~~text
React/Vite desk
  -> typed local HTTP client
  -> FastAPI routes
       /health       application liveness
       /readiness    read-only, fail-closed authorization diagnostic
       /agents       exact nine-specialist roster
       /markets      explicit mock fixtures
       /analyze      deterministic mock orchestration

provider connectors
  -> provider-neutral normalized events
  -> bounded immutable evidence snapshots
  -> specialist routing
  -> structured specialist analyses
  -> Master synthesis and non-executable thesis/proposal boundaries
  -> Sentinel policy and risk gates
  -> disabled Pilot / guarded execution architecture
~~~

The evidence, proposal, risk, authority, reservation, audit, and recovery
layers have separate contracts and tests. Their existence does not activate an
order path. Public API routes expose no execution operation.

## Capability and gap matrix

| Capability | Current implementation | Remaining gap | Safety consequence |
| --- | --- | --- | --- |
| Specialist desk | Nine canonical specialists plus FRIDAY Master | Real model provider is not configured | Analysis stays deterministic and labeled mock |
| Market display | Explicit mock API and mock fallback fixtures with finite, coherent, mock-only response validation | No live feed is connected to the dashboard | Values are reference fixtures, never presented as live |
| Evidence | Provider-neutral immutable domains, bounded stores, provenance | External sources remain capability-specific and manually integrated | Missing evidence fails closed; nothing is fabricated |
| Analysis | Deterministic routing and structured specialist artifacts | No production AI model/tool authority | No agent can gain execution authority |
| Operational status | Liveness, correlation IDs, fail-closed readiness report, direct-peer loopback enforcement, browser-origin filtering, and bounded request bodies | Dependency-specific live probes and authenticated non-loopback ingress are intentionally absent | Diagnostics make no broker/provider calls; remote direct clients fail closed |
| Trust | Personal signing, DPAPI custody, local bootstrap, canonical Ed25519 verification, atomic local SQLite replay consumption, and strict signed anchor-lifecycle/witness contracts | Independent provisioning of the governance root, an authenticated lifecycle authority, and an external monotonic witness remain unconfigured | Cryptographic structure, lifecycle lineage, and local duplicate prevention can be checked, but none establishes independent authority or production authorization |
| Broker state | Read-only typed evidence/reconciliation contracts and guarded MT5 adapter architecture | The evidence provider is not independently authenticated and reconciliation is not authoritative | Structurally valid evidence is explicitly insufficient for production readiness |
| Execution | Sentinel, reservations, kill switch, audit and final gates | Organizational approval and reviewed production boundary are absent | Live and demo submission remain disabled |
| Persistence | Bounded evidence stores plus durable SQLite safety domains | Stores intentionally do not form one external atomic transaction | Ambiguity requires fail-closed reconciliation |
| Frontend | Typed health/readiness/roster/market/analysis integration | No browser test framework is configured | Lint, type-check and production build are the current UI gate |

## Local Windows workflow

Backend:

~~~powershell
cd backend
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
~~~

Frontend, in a second terminal:

~~~powershell
cd frontend
npm install
npm run dev
~~~

Set VITE_API_BASE_URL only when the backend uses a different local address.
The default is http://127.0.0.1:8000.

Verification:

~~~powershell
cd backend
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m compileall -q app tests

cd ..\frontend
npm run lint
npm run build
~~~

## Diagnostic interpretation

- GET /api/health means only that the HTTP application is responding.
- GET /api/readiness enumerates production blockers without probing MT5,
  reading credentials, consuming approvals, or mutating lifecycle state.
- The application rejects direct non-loopback peers and unapproved browser
  origins. This is a local deployment guard, not user authentication; do not
  place an unauthenticated reverse proxy in front of it.
- Request bodies are bounded to 1 MiB at both declared-length and streamed-body
  boundaries.
- Local replay readiness distinguishes restart-durable/atomic SQLite consumption
  from unavailable external rollback assurance. A local database is not an
  independently protected monotonic witness.
- Phase 15 validates signed anchor identity, generation, scope, rotation,
  revocation, validity, and policy lineage against a separately configured root.
  Because that root is not independently authenticated, valid local evidence is
  still configured-but-unauthenticated. The external witness is unavailable by
  default and cannot be replaced by local state.
- Phase 16 adds a no-I/O-by-default, provider-neutral attestation boundary for
  governance-root provisioning, authoritative lifecycle retrieval, and broker
  evidence provenance/reconciliation. It validates real Ed25519 signatures
  against separately configured public material, but deliberately reports
  configured-but-unauthenticated because no independently provisioned root or
  operational external provider exists. Readiness reports these dependencies
  separately and remains blocked.
- Mock market values are explicitly labeled in both the API and UI.
- The analysis endpoint reports deterministic_mock; it is not a live AI
  provider.
- Correlation IDs are transport diagnostics, not authority or audit proof.

## External blockers

The repository cannot create these prerequisites for itself:

1. an independently established and governed trust anchor;
2. authenticated approval, revocation, rotation, and status authority;
3. externally durable replay and rollback protection;
4. trusted, fresh broker/account/instrument evidence;
5. authoritative broker reconciliation;
6. an explicit production submission-boundary security review and human
   authorization.

Until those are supplied and independently verified, production authorization,
submission, and execution remain blocked.

The detailed contract and limitation analysis is in
[`independent-trust-and-broker-evidence.md`](independent-trust-and-broker-evidence.md).
