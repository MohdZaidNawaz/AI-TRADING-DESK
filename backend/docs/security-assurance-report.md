# FRIDAY Security Assurance and Release-Readiness Audit

Date: 2026-10-21

Phase 14 baseline: `675335be73abb220c6ce7c1188090ef39f626bfd`

Scope: current repository implementation, local API, browser dashboard, and
non-submitting execution-safety boundaries.

## Executive conclusion

FRIDAY is **not authorized for production, demo submission, or live execution**.
The repository continues to fail closed when independent authority, externally
durable replay protection, authenticated broker evidence, authoritative broker
reconciliation, explicit execution authorization, or the production submission
review is absent. No audit diagnostic, request parameter, browser control, local
signature, DPAPI record, SQLite record, or test double can satisfy those missing
external prerequisites.

No critical or high-severity implementation vulnerability was demonstrated in
the reviewed trust and readiness path. Two bounded defects were corrected:

1. Broker evidence could be described as structurally complete without a source
   retrieval timestamp, and retrieval chronology was not validated.
2. The structured-log helper rejected credential-shaped field names but did not
   reject a credential-shaped value stored under an innocuous name.

The audit does not claim absolute security. It establishes only the properties
supported by the code paths and tests listed below.

## Scope and methodology

The review traced data and control flow through:

- Windows DPAPI private-key protection, restrictive ACL verification, lifecycle
  locking, rotation, revocation, and recovery.
- Personal policy signing and personal trust-bootstrap assessment.
- Independently configured Ed25519 verification and replay-protection seams.
- Provider-neutral, read-only broker/account evidence validation.
- Production-readiness models, service aggregation, and `/api/readiness`.
- FastAPI route registration, CORS, correlation IDs, provider routes, and input
  models.
- React API calls, mock-market labeling, readiness display, and the absence of
  browser execution controls.
- Static reachability searches for Pilot, FinalExecutionGate, MT5 initialization,
  `order_send`, and execution endpoints.
- Negative tests for substituted anchors, altered signed payloads, cross-lineage
  reuse, chronology, stale evidence, replay/rollback, terminal key states,
  readiness input, and broker-evidence incompleteness.

The audit did not read `.env`, initialize MT5, contact a provider or broker, or
perform an account/order operation.

## Findings

### SA-01 — broker retrieval provenance was incomplete (medium, remediated)

**Evidence.** `ProviderExecutionEvidence.retrieved_at` is a distinct source-
retrieval timestamp. Before remediation,
`TrustedExecutionEvidenceService._validate()` checked source observation and
market timestamps but did not require or validate retrieval time, while
`ValidatedExecutionEvidence` could call such a result structurally complete.

**Consequence.** This could not grant execution authority: provider evidence is
still explicitly `UNAUTHENTICATED`, and the production-readiness gate rejects it.
It could, however, overstate the diagnostic completeness and freshness of a
broker snapshot.

**Remediation.** `app/services/trusted_execution_evidence.py` now requires a
retrieval timestamp, rejects retrieval before source observation, future/stale
retrieval, market observation after retrieval, and portfolio-risk observation
after retrieval. `app/models/trusted_execution_evidence.py` also requires
retrieval provenance whenever `ready=True`. Adversarial coverage is in
`tests/test_trusted_execution_evidence.py`.

### SA-02 — credential-shaped log values were not filtered (low, remediated)

**Evidence.** `app/observability/events.py` filtered sensitive field names but
previously allowed values such as `token=...` under a generic key. No production
caller of this helper was found, so no actual disclosure was observed.

**Consequence.** A future caller could have defeated the intended structured-log
contract by choosing a harmless-looking key.

**Remediation.** Credential-assignment patterns are now rejected in field values
as well as names. `tests/test_observability.py` asserts that those values do not
reach log output. This is defense in depth, not a promise that arbitrary
unlabeled secret bytes can be recognized automatically; callers must still never
submit secrets to logging APIs.

### SA-03 — independent production authority is not established (release blocker)

**Evidence.** `ConfiguredEd25519TrustAnchor` accepts only an independently
configured Ed25519 public key and the signed authority envelope cannot supply a
replacement verification key. `IndependentKeyStatusVerifier` binds authority,
anchor ID/version/fingerprint, request fingerprint, challenge, subject key and
generation, exact scope, chronology, and sequence before signature verification.
Terminal revoked, superseded, compromised, and recovery-required states remain
terminal. However, configuration is explicitly
`independently_trusted=False`; verification results are structurally
non-authorizing; and the default replay protector is unavailable. Phase 14 adds
an explicitly initialized local SQLite replay store whose claim, monotonic
stream head, and history event commit atomically. Physical integrity, exact
schema, foreign keys, canonical claim/stream fingerprints, maximum-sequence
heads, and one-to-one deterministic claim history are validated without repair
before state is trusted. It is not enabled by default and is not an independent
authority.

**Assessment.** The cryptographic binding is implemented and tested, including
wrong-anchor, substituted-key, altered-payload, cross-authority, cross-subject,
cross-generation, cross-scope, stale/future, replay, and rollback cases. What is
missing is operationally independent trust-anchor governance and an externally
protected monotonic replay/rollback witness. Local SQLite now prevents duplicate
consumption across processes sharing the database and survives ordinary restart,
and semantic contradictions within the current database fail closed. A coherent
privileged local snapshot rollback remains undetectable. Local DPAPI, SQLite,
local clocks, and local signatures do not supply independent governance or
privileged-host rollback resistance.

### SA-04 — personal key protection is local protection, not independent trust
(release blocker)

**Evidence.** `WindowsDpapiProtector` uses current-user DPAPI with UI forbidden,
bounded native buffers, and cleanup on native and Python failure paths.
`WindowsRestrictedAcl` applies and compares a protected DACL restricted to the
current user, SYSTEM, and Administrators. `WindowsDpapiKeyProvider` validates
metadata-bound entropy, public/private binding, exact ACLs, and a cross-process
lifecycle lock; corrupt or undecryptable records fail closed instead of creating
replacement keys.

**Assessment.** This protects a local exportable Ed25519 key at rest for the
current Windows user. It is neither hardware-backed nor an independently
governed revocation authority. Python-level copies cannot be guaranteed fully
erased. Personal trust ceremonies and recovery assessments remain local,
diagnostic, and structurally non-authorizing.

### SA-05 — broker evidence remains unauthenticated and reconciliation unavailable
(release blocker)

**Evidence.** `ValidatedExecutionEvidence` can represent only `UNAVAILABLE` or
`UNAUTHENTICATED` source authentication. The service validates account, symbol,
instrument capabilities, quote consistency, portfolio lineage, freshness, and
now retrieval chronology, but it cannot authenticate the broker source.
`ProductionAuthorizationReadinessGate._trusted_evidence()` always blocks even a
fresh, structurally complete observation. Broker reconciliation likewise always
reports unavailable unless an unresolved state is already known, in which case
it reports blocked.

**Assessment.** Typed evidence is useful for diagnostics and future integration,
but it is not authoritative broker evidence and cannot authorize execution.

### SA-06 — readiness is non-authorizing (verified)

**Evidence.** `ProductionAuthorizationReadinessReport` fixes status to `blocked`
and all authority/submission flags to literal `False`. Every prerequisite must
appear exactly once at one evaluation timestamp. `/api/readiness` accepts no
authority input and constructs a default fail-closed gate; extra query parameters
cannot change its result. The route is GET-only and no execution router is
registered.

The React client only invokes health, readiness, agents, mock markets, and
deterministic analysis. It labels market data as mock/not tradeable and execution
as blocked/off/non-executing. No browser submission control or execution API
client exists. Client labels are not treated as a server-side security boundary.

### SA-07 — local API exposure boundary (medium residual, partially remediated)

**Evidence.** The application rejects direct non-loopback peers, rejects
unexpected browser origins, disables CORS credentials, and bounds declared and
streamed bodies to 1 MiB. CORS remains restricted to the two local Vite origins,
methods to GET and POST, and a small header allow-list. Request models bound
symbols, queries, and provider parameters. Provider failures return generic
messages. Documentation runs Uvicorn on `127.0.0.1` with access logging disabled.

**Residual risk.** The application does not implement user authentication or a
trusted reverse-proxy boundary. CORS is a browser policy, not authentication. A
same-host reverse proxy would appear as a loopback peer and must not be deployed
without authentication, trusted-proxy rules, rate limits, and route-level
authorization. The webhook secret is carried in the URL by established design
and can be exposed by proxy, browser, or server access logs if operational
logging differs from the documented command. Interactive API documentation is
also enabled for the local development boundary.

**Required control.** Keep the API directly loopback-only until authenticated
network ingress, rate limits, secret-safe logging, and route-level authorization
are designed and reviewed. Do not regard CORS or the direct-peer check as an
authenticated proxy boundary.

### SA-08 — raw connector payloads require containment (low residual risk)

**Evidence.** Provider connectors normalize payloads before evidence projection;
`MarketEvidence.from_event()` explicitly omits `raw_payload`. Twelve Data and
Alpha Vantage connector sanitizers remove credential-like fields and echoed API
keys. The TradingView normalized event retains the caller-supplied raw payload in
the bounded process-local ingestion history, though the safe evidence projection
does not carry it to specialists.

**Residual risk.** Operators and future developers must not log or expose raw
connector payloads. The manual Twelve Data endpoint returns the sanitized
normalized event, including its sanitized raw payload, and therefore must remain
local. Unknown TradingView fields may contain caller-supplied sensitive data even
though the authentication token itself is not copied into the payload.

## Security invariants supported by evidence

- A response cannot establish its own verification key: the Ed25519 key comes
  from a separately constructed anchor, and the signed envelope forbids extras.
- Authority signatures bind the complete request subject, scope, anchor,
  chronology, challenge, and sequence before a result is accepted.
- Replay results must bind the exact statement, key generation, scope, sequence,
  and evaluation timestamp. The optional local SQLite store consumes claims
  atomically and durably across restart; missing external rollback assurance
  remains a distinct fail-closed readiness blocker.
- Revoked, superseded, compromised, recovery-required, stale, replayed, rollback,
  conflict, and unavailable results never fall through to an active outcome.
- Local recovery can diagnose state but cannot create, reactivate, replace, or
  authorize a key.
- Broker evidence cannot claim authenticated source authority in the current
  contract and cannot satisfy production readiness.
- Request parameters cannot make readiness or execution-authority flags true.
- No registered API route reaches Pilot, FinalExecutionGate, an MT5 adapter, an
  execution-state mutation, or broker submission.
- Errors reviewed in provider/API boundaries use generic messages; private-key
  operations suppress backend/native detail; structured logging rejects known
  credential-shaped names and assignment values.

## Validation

Validation was performed against the final Phase 14 working tree using the
project virtual environment:

- Focused Phase 14 security/regression suite:
  `python -m pytest` over durable independent replay, Windows DPAPI, personal
  signing/trust, independent trust, production readiness, operational API, and
  observability tests — **210 passed**.
- Full backend: `python -m pytest -q` — **1059 passed**, zero failures or skips.
- Python compilation: `python -m compileall -q app tests` — passed.
- Patch hygiene: `git diff --check` — passed.

No frontend contract changed in Phase 14, so frontend validation was not rerun.

## Phase 15 trust governance and rollback assurance

Phase 15 adds canonical signed trust-anchor governance manifests and a
provider-neutral external rollback-witness contract. The manifest verifier uses
only a separately configured Ed25519 governance-root key; evidence cannot
supply or replace that key. Exact authority/root/anchor identity, fingerprint,
generation, scope, policy, request, challenge, sequence, validity, lifecycle,
and rotation lineage are checked before a result is composed.

This is not an independent authority integration. The configured governance
root remains explicitly unauthenticated local configuration, so even a valid
signature produces `CONFIGURED_UNAUTHENTICATED`. Revocation, expiry, invalid
rotation, downgrade, missing governance, and contradictory lineage fail closed.
No recovery path recreates trust from local state.

Rotation chronology is enforced at two explicit version-1 boundaries. The
predecessor must be valid when the successor manifest is issued, and it cannot
expire before the successor transition time. Key overlap is allowed, as is an
exact zero-overlap expiry/activation boundary; any validity gap, future-dated
predecessor, or successor that predates its predecessor is invalid. A
historically valid predecessor may be expired at current evaluation without
making the chain invalid, but that historical result remains non-authoritative.

The rollback-witness interface binds trust generation and the exact replay
checkpoint, but no authenticated external monotonic provider is configured.
Default and synthetic providers cannot set authentication, monotonicity, or
external-assurance flags to true. Local SQLite durability remains distinct from
external rollback protection and still cannot detect restoration of a coherent
older database snapshot.

Readiness now reports local key custody and anchor lifecycle separately and
remains structurally blocked. No Phase 15 component imports or invokes Sentinel,
Pilot, FinalExecutionGate, MT5, an adapter, an API execution route, or order
submission.

## Remaining external blockers

1. Independently governed trust-anchor bootstrap, rotation, and revocation.
2. A genuinely authenticated external key-status authority.
3. An independently protected monotonic replay/rollback witness; the local
   atomic store does not resist privileged snapshot restoration.
4. Authenticated, fresh broker/account/instrument/position evidence with durable
   provenance.
5. Authoritative broker reconciliation for uncertain outcomes and restarts.
6. Route/network authentication and deployment controls if the API is ever
   exposed beyond loopback.
7. Independent production review and explicit authorization of the submission
   boundary.

Until every blocker is implemented, independently verified, and operationally
configured, **production authorization is not permitted**. Demo and live order
submission must remain disabled.

## Phase 16 authenticated external trust boundary

Phase 16 adds immutable signed-attestation contracts and a read-only provider
protocol for root provisioning, lifecycle status, and broker-evidence
provenance. The verifier uses only separately configured Ed25519 public
material; an attestation cannot provide or substitute its own verification
key. Exact provider, root, request, challenge, capability, subject generation
and fingerprint, scope, policy, sequence, chronology, expiry, lifecycle, and
reconciliation bindings are checked at composition time.

This is an integration boundary, not an operational authority. The existing
configured-root model fixes independent trust to false, and Phase 16 assessment
models fix independent authentication, authoritative lifecycle, external
rollback assurance, broker authentication, authoritative reconciliation,
production authorization, submission, and execution to false. A valid local
signature therefore remains `CONFIGURED_UNAUTHENTICATED`.

Readiness now distinguishes governance-root provisioning, rotation/recovery
assurance, and authenticated broker evidence from the pre-existing trust,
replay, structural evidence, reconciliation, review, and authorization checks.
No new check can become satisfied from a test fake or locally configured key.

No authenticated provisioning authority, lifecycle provider, external
monotonic witness, or broker-evidence provider was available or contacted.
Operational independent trust, current revocation assurance, privileged-host
rollback resistance, complete broker truth, and authoritative reconciliation
remain external release blockers. See
[`phase16-authenticated-external-trust.md`](phase16-authenticated-external-trust.md).
