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

### Phase 18 — durable external-attestation consumption (implemented, non-authorizing)

Phase 18 binds signed broker attestations to the exact typed evidence and
matched reconciliation snapshot used by readiness. It atomically consumes a
global challenge binding and a provider-sequence binding in the existing local
Phase 14 replay database, adds bounded provider-call failure behavior, and
hardens structured identifier logging. No schema migration or external service
was added. The configured verification root remains independently untrusted,
local SQLite has no external rollback assurance, readiness remains blocked, and
all submission paths remain disabled.

### Phase 19 — external trust operations (implemented locally, externally unavailable)

Phase 19 extends the existing signed-attestation protocol with exact rollback-
witness and authoritative-reconciliation lineages and composes root,
lifecycle, witness, broker, and reconciliation assessments into one immutable
non-authorizing diagnostic. It reuses Phase 18 atomic replay consumption and
adds no network client, credential, execution route, or persistence schema.

All operational independence claims remain false. Real root provisioning,
authenticated transport and lifecycle service, an independently administered
monotonic witness, broker evidence, authoritative reconciliation, and security
approval remain external blockers. See
[`phase19-external-trust-operations.md`](phase19-external-trust-operations.md).

### Phase 20 — operational readiness (implemented locally, deployment blocked)

Phase 20 adds an immutable, deterministic operational-readiness diagnostic for
configuration, independent trust/lifecycle/witness services, broker evidence,
reconciliation, replay health, clocks, provider limits, audit/redaction,
monitoring, recovery, security review, human approval, and execution controls.
The evaluator is explicitly an offline operational acceptance assessment, not
the existing runtime readiness checker or `/api/readiness` production-
authorization diagnostic. Caller-shaped local, synthetic, external, and manual
claims cannot satisfy gates. Only the exact durable replay store's read-only
diagnostics currently supply a concrete verified local observation. A valid
configuration policy remains runtime-unverified. The aggregate remains blocked
and all authority fields are literal false.

Deployment topology, configuration acceptance, monitoring, backups, incident
response, external integration gates, and the release checklist are specified
in [`phase20-operational-readiness.md`](phase20-operational-readiness.md). No
external service, deployment automation, credential, API route, or execution
path was added. Staging and production remain unprovisioned.

### Phase 21 — external dependency acceptance (implemented locally, integrations unavailable)

Phase 21 adds a canonical nine-dependency registry, a sanitized provider-neutral
evidence envelope, discriminated dependency-specific payloads, and a
deterministic offline acceptance workflow. It aligns with the existing trust,
attestation, replay, broker-lineage, readiness, and bounded-observability
contracts without treating them as authenticated input. Caller status strings,
booleans, reason codes, fingerprints, and
claimed independent provenance cannot establish acceptance. Structurally valid
synthetic fixtures are labeled synthetic-tested and retain an explicit
non-operational blocker.

Broker, reconciliation, monitoring, recovery, security-review, and human-
approval payloads now have locally enforced completeness and policy rules.
Security review and approval require exact release-commit binding. These typed
payloads remain sanitized summaries: no signature, trust chain, issuer, receipt,
capability, reviewer, or approver becomes trusted without a real independent
verifier.

The environment model distinguishes local development, deterministic tests,
isolated sandbox, independently verified pre-production, and production.
Phase 21 performs no automatic promotion and cannot emit independently-verified
or accepted results. It is not connected to `/api/readiness`, Pilot,
FinalExecutionGate, MT5, providers, deployment, or execution. Real trust-root
provisioning, lifecycle authority, rollback witness, broker evidence,
reconciliation, monitoring delivery, recovery rehearsal, independent review,
and human approval remain blocked. See
[`phase21-external-integration-acceptance.md`](phase21-external-integration-acceptance.md).
