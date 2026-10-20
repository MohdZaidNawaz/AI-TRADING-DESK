# FRIDAY Security Assurance and Release-Readiness Audit

Date: 2026-10-20

Audited baseline: `ed6798e2f84d199d778777388e215df9cee89ced`

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
non-authorizing; and the default replay protector is unavailable.

**Assessment.** The cryptographic binding is implemented and tested, including
wrong-anchor, substituted-key, altered-payload, cross-authority, cross-subject,
cross-generation, cross-scope, stale/future, replay, and rollback cases. What is
missing is operationally independent trust-anchor governance and an externally
durable replay/rollback service. Local DPAPI, SQLite, local clocks, and local
signatures do not supply those properties.

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

### SA-07 — local API exposure assumptions require operational enforcement
(medium residual risk)

**Evidence.** CORS is restricted to the two local Vite origins, methods to GET
and POST, and a small header allow-list. Request models bound symbols, queries,
and provider parameters. Provider failures return generic messages. Documentation
runs Uvicorn on `127.0.0.1` with access logging disabled.

**Residual risk.** The application does not implement user authentication or
host-level network access control. CORS is a browser policy, not authentication.
If an operator binds the API to a non-loopback interface, local diagnostics,
analysis, the TradingView webhook, and the manually triggered Twelve Data
snapshot route acquire an unauthorized remote attack/usage surface. The webhook
secret is carried in the URL by established design and can be exposed by reverse
proxy, browser, or server access logs if operational logging differs from the
documented command. Interactive API documentation is also enabled.

**Required control.** Keep the current API loopback-only until authenticated
network ingress, rate limits, secret-safe logging, and route-level authorization
are designed and reviewed. Do not regard the CORS list as that control.

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
  and evaluation timestamp. Missing external replay infrastructure fails closed.
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

Validation was performed against the final working tree using the project
virtual environment and existing frontend toolchain:

- Focused security/regression suite:
  `python -m pytest` over Windows DPAPI, personal signing/trust, independent
  trust, trusted evidence, production readiness, operational API,
  observability, replay, execution-safety, final-gate, and Pilot tests —
  **218 passed**.
- Full backend: `python -m pytest -q` — **1022 passed**, zero failures.
- Python compilation: `python -m compileall -q app tests` — passed.
- Frontend lint: `npm run lint` — passed.
- Frontend type-check and production build: `npm run build` — passed; 25 modules
  transformed.
- Patch hygiene: `git diff --check` — passed.

The first full regression attempt failed because older synthetic test providers
did not supply the newly required retrieval timestamp. Those fixtures were
updated to provide their already-known capture time; no production validation
was weakened. The final full suite above was rerun after that correction. The
first sandboxed frontend build also hit a Windows `spawn EPERM`; the identical
build completed successfully under the normal permitted process boundary.

## Remaining external blockers

1. Independently governed trust-anchor bootstrap, rotation, and revocation.
2. A genuinely authenticated external key-status authority.
3. Externally durable replay/rollback protection with concurrency and recovery
   guarantees.
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
