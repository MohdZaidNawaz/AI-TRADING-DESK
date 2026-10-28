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

## Phase 18 durable attestation and exact evidence-lineage hardening

Phase 18 retains the complete normalized signed broker statement in its
non-authorizing assessment and compares the signed evidence request, evidence
snapshot, source-scoped account, source timestamps, and matched reconciliation
fingerprints with the exact objects supplied to readiness. Unavailable or
mismatched context is invalid; a correct match remains blocked because the
verification root and provider are not independently authenticated.

The Phase 14 replay database is extended through an atomic multi-binding API,
not a new schema or competing store. Each valid attestation consumes a globally
scoped challenge stream and a provider/root/subject/scope/policy sequence stream
within one transaction. Claims, heads, and events either commit together or are
reported unavailable. This provides local concurrency and restart durability,
but it does not detect coherent rollback and is not external monotonic proof.

Provider calls are time-bounded and globally capacity-bounded. A timed-out
Python thread cannot be terminated safely, so it may finish later; its result is
discarded and the bounded slot is released only when it exits. Exceptions,
timeouts, and capacity exhaustion return generic unavailable codes. No network
client, credential, retry, or cached-success behavior is present.

All production-authorization, submission, execution, independent-authentication,
authoritative-reconciliation, and external-rollback fields remain literal false.
Operational trust-root provisioning, lifecycle/revocation service, external
witness, authenticated broker provider, authoritative reconciliation, and
independent production review remain blockers.

## Phase 19 external trust operations assurance

Phase 19 reuses the Phase 16/18 Ed25519, exact-binding, freshness, timeout, and
atomic replay controls for five separate capabilities: root provisioning,
lifecycle status, rollback witness, broker evidence, and authoritative
reconciliation. Witness source sequences are bound to signed checkpoint
sequences. Reconciliation signs exact account, scope, evidence, snapshot,
reservation, execution-history, generation, coverage, and outcome lineage.

The aggregate operational assessment accepts only complete locally verified
lineage, yet remains `STRUCTURALLY_VALIDATED_UNAUTHENTICATED`. Its independent
root, authoritative lifecycle, transport authentication, external rollback,
broker authentication, authoritative reconciliation, production authorization,
submission, and execution fields are all literal false. Readiness maps even a
complete synthetic result to `BLOCKED`, never `SATISFIED`.

No network transport, provider credential, private key, broker connection,
external witness, retry loop, execution adapter, or schema migration was added.
Local replay state still cannot detect coherent restoration without a real
independently administered witness. The detailed crash and recovery model is in
[`phase19-external-trust-operations.md`](phase19-external-trust-operations.md).

## Phase 20 operational-readiness assurance

Phase 20 readiness contracts are immutable, extra-forbid, deterministically
ordered, and secret-reference rejecting. Missing, conflicting, future-dated,
stale, invalid, unavailable, and synthetic evidence fail closed. Only a
concrete read-only diagnostic from the exact durable replay store can currently
be marked satisfied. Caller-created local, synthetic, external, and manual
claims remain blocked. Structurally valid configuration remains blocked until
runtime enforcement is observed. The aggregate is always blocked, and
production authorization, submission, and execution are literal false.

Replay diagnostics are invoked through the concrete class implementation,
not a shadowable instance attribute. Exact model type, evaluation timestamp,
validated fields, and canonical fingerprint are rechecked; malformed or
exceptional results become sanitized blocked diagnostics. This is local process
and SQLite integrity evidence only. Mutable host state and coherent rollback
remain outside its trust guarantee.

Operational signals use a bounded vocabulary and the existing identifier and
field redaction boundary. Signal, state, and reason labels are finite enums;
their exact enum types are required and foreign enums or value-like objects are
rejected. No raw statement, provider payload, broker response,
credential, or private material is accepted. The operations design explicitly
treats monitoring failure, recovery uncertainty, witness disagreement, and
restored local state without external checkpoint verification as blocking.

This is local contract and synthetic-test assurance only. Independent service
provisioning, alert delivery, backup rehearsal, vulnerability intelligence,
organizational access controls, security review, and human approval remain
external blockers. See
[`phase20-operational-readiness.md`](phase20-operational-readiness.md).

The Phase 20 evaluator is an offline assessment only. Existing runtime
readiness and `/api/readiness` behavior are unchanged, and no Phase 20 module is
referenced by Pilot, FinalExecutionGate, MT5, broker adapters, or API routes.

## Phase 21 external-dependency acceptance assurance

Phase 21 provides a complete immutable registry for the nine outstanding
external dependencies and a sanitized evidence envelope bound to dependency,
issuer, subject, environment, request, challenge, scope, chronology, sequence,
typed payload fingerprint, policy version, and required release commit.
Independent-security-review and human-production-approval evidence must bind
the exact assessed commit in both envelope and typed payload. Canonical hashes prove
deterministic local equality only; they are never described as signatures,
authenticity, or independent trust.

The offline evaluator rejects missing, malformed, stale, future-dated,
revoked, duplicate, conflicting, incorrectly scoped, policy-mismatched, and
missing/commit-mismatched evidence. Typed local policy checks cover broker field
and capability metadata, reconciliation coverage, monitoring receipts, recovery
objectives and consistency, review independence/findings, and approval scope,
lifecycle, and decision. Caller-provided verification results and reason codes
are not treated as policy findings. Synthetic fixtures can demonstrate local validation
but cannot become independent acceptance. Even a well-formed envelope claiming
independent verification remains blocked because no independently configured
verifier exists. All dependency assessments and the aggregate report retain
literal-false authority, submission, and execution fields.

The evaluator constructs the canonical registry internally and never invokes
caller equality or uses caller-supplied registry contents. Recovery evidence is
additionally bound to evaluation time, the inclusive envelope window, and the
registry's 30-day completion freshness limit. A fresh envelope cannot refresh
an old rehearsal, and the exact freshness cutoff remains valid. Future or
envelope-window-invalid recovery timestamps classify as invalid even when the
envelope is stale; revoked identity remains the terminal higher-precedence
state.

The public evaluation boundary also requires the exact acceptance-context type
and inspects a defensive copy of its raw field state without dispatching through
instance serialization methods. Missing or undeclared fields, non-empty extra
state, shadowed serializer attributes, wrong types, malformed identifiers, and
naive timestamps fail with a deterministic validation error. This protects the
documented Pydantic boundary from supported validation-bypassing copy cases; it
does not claim protection from arbitrary in-process code execution. General
single-envelope maturity uses an explicit
`revoked > invalid > stale > unavailable > local_contract` precedence instead
of branch assignment order. Recovery retains its documented special handling
for timestamp-invalid versus merely old rehearsal evidence. Neither
classification path can produce independent verification, acceptance,
authorization, submission permission, or execution authority.

Phase 21 extends the existing bounded operational-event enums for dependency
evidence, identity, sequence, invalidation, alert delivery, recovery rehearsal,
and approval state. It adds no raw payload or exception logging. The service
has no provider, network, API, Pilot, FinalExecutionGate, MT5, broker, replay-
mutation, deployment, or execution integration.

This assurance remains local and synthetic. Trust-root provisioning,
authoritative lifecycle/revocation, independent rollback witnessing,
authenticated read-only broker evidence, authoritative reconciliation,
verified monitoring delivery, recovery rehearsal, independent security review,
and authenticated human production approval remain unavailable. See
[`phase21-external-integration-acceptance.md`](phase21-external-integration-acceptance.md).

## Phase 22 offline evidence-verification assurance

Phase 22 uses PyCA Ed25519 to verify domain-separated canonical Phase 21
evidence against an explicitly supplied immutable public-key configuration. The
signed bytes bind the complete evidence envelope plus algorithm, key identity
and generation, configured root version/fingerprint, and Phase 22 policy. The
service independently rechecks exact subject, request, challenge, environment,
scope, Phase 21 policy, release commit, evidence type, freshness, and fixed
evaluation time.

The Phase 21 evidence boundary and Phase 22 configuration/request boundary now
require exact model classes, defensively copy exact declared raw fields, reject
missing or undeclared fields and non-empty extra state, and independently
reconstruct nested payload, trust-anchor, context, signed-envelope, and evidence
models before policy or cryptographic evaluation. Caller-shadowed serializers
are never invoked. Structured logging applies the same boundary before safe
serialization, so validation-bypassing copies are revalidated and redacted.
These defenses cover malformed in-process model objects; they are not a claim
of protection against arbitrary malicious code already running in the process.

There is no default root, algorithm fallback, network provider, or private-key
operation in production code. Evidence-supplied keys and caller claims cannot
establish trust. Configuration is restricted to synthetic non-production use,
and valid signatures report configured-key authentication only. Independent
root governance, lifecycle/revocation authority, and external replay/rollback
protection remain unavailable blockers.

Reports use bounded enums and retain no raw secret or broker payload. All
independent-verification, operational-acceptance, production-authorization,
submission, and execution fields are literal false. The Phase 22 modules have
no runtime readiness, API, Pilot, FinalExecutionGate, MT5, broker, or execution
integration. See
[`phase22-independent-evidence-verification.md`](phase22-independent-evidence-verification.md).

Phase 23 locks the existing fail-closed precedence and temporal boundaries with
real-verifier regression tests. Binding errors precede freshness failures,
which precede untrusted evidence-lifecycle claims; all computed lifecycle and
freshness reasons remain visible. Configured-root validity starts inclusively
and expires exclusively, evidence expiry is exclusive, and the canonical
registry's maximum evidence age is inclusive. Canonical signatures cover the
validated normalized model rather than the original transport byte encoding.
None of these local checks authenticates lifecycle claims or supplies external
replay protection.

Phase 23 also separates evidence/context binding from trust-root temporal and
lifecycle state. Identity or permission mismatches remain binding-invalid, but
a not-yet-valid, expired, revoked, superseded, compromised, or unknown root
preserves a previously valid evidence binding and fails through its disposition,
issuer/lifecycle status, and bounded root reason. All such outcomes remain
non-authorizing.
