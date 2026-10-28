# Phase 21: external integration acceptance

## Verified checkpoint and boundary

Phase 21 started from `f1892f15e98f53430049e2f702f01eb8d370bd59`
on `main`, synchronized with `origin/main`. It adds a local, deterministic,
offline acceptance framework. It does not connect an external authority,
monitoring service, rollback witness, broker, or reconciliation source.

The implementation reuses the existing Phase 14–20 vocabulary and boundaries:

- signed external attestation and governance contracts;
- local atomic SQLite replay diagnostics;
- broker-evidence and reconciliation terminology and lineage requirements;
- offline operational readiness and literal-false authority fields;
- bounded operational events and redaction.

It is not wired to `/api/readiness`, `OperationalReadinessChecker`, Pilot,
`FinalExecutionGate`, MT5, broker adapters, deployment, or submission.

## Maturity vocabulary

| Maturity | Meaning |
|---|---|
| `designed` | Requirements exist, but no local contract is implemented. |
| `local_contract` | Immutable local schema and validation exist. |
| `synthetic_tested` | Deterministic fixtures exercised local validation only. |
| `sandbox_connected` | A sandbox transport was connected, without independent acceptance. |
| `independently_verified` | An independently configured verifier authenticated current evidence. |
| `accepted` | An authorized decision accepted independently verified evidence under the exact policy and release. |
| `unavailable`, `stale`, `invalid`, `conflicting`, `revoked` | Fail-closed states that cannot be promoted. |

The local Phase 21 evaluator can produce local-contract, synthetic-tested, and
failure states. It cannot produce `independently_verified` or `accepted`.
Caller-supplied booleans, status strings, signatures, fingerprints, and claimed
provenance are data, not trust.

## Dependency registry

Every registry entry has a stable ID, security boundary, required evidence,
owner role, verification method, freshness limit, exact identity/scope
requirements, failure modes, acceptance criteria, verification mode, allowed
environments, blocker codes, and retention/redaction policy.

The evaluator always constructs this canonical registry internally. Its legacy
constructor argument is compatibility-only and is never compared with or used
as policy, so caller-controlled equality or registry contents cannot weaken the
active requirements.

| Dependency | Required external acceptance evidence | Current state |
|---|---|---|
| Independent trust root | Out-of-band provisioned root identity, algorithm, scope, validity, rotation and revocation history | local contract; provisioning unavailable |
| Lifecycle/revocation authority | Authenticated, fresh, exact-subject lifecycle status with monotonic version | local contract; authority unavailable |
| Monotonic rollback witness | Independently administered, authenticated checkpoint confirmation bound to stream and challenge | local contract; witness unavailable |
| Read-only broker evidence | Authenticated complete snapshot bound to account scope, source generation, and freshness | local contract; provider unavailable |
| Authoritative reconciliation | Authenticated complete reconciliation decision with no unresolved discrepancy | local contract; authority unavailable |
| Monitoring/alert delivery | Bounded signal coverage and authenticated delivery receipt with an escalation owner | local contract; delivery unverified |
| Backup/recovery rehearsal | Integrity evidence, restoration exercise, RPO/RTO result, and replay/witness consistency | local contract; rehearsal missing |
| Independent security review | Independent reviewer identity, exact commit/scope, findings, remediation, and decision | local contract; review pending |
| Human production approval | Authenticated role, exact commit/scope/policy, expiry, and revocation status | local contract; approval absent |

No registry entry authorizes execution. Registry fingerprints prove canonical
local content equality only; they do not prove authenticity.

## Evidence envelope

`ExternalDependencyEvidenceEnvelope` stores sanitized metadata only:

- dependency and evidence type;
- issuer, subject, environment, request, and challenge identities;
- scope, payload, signature, and trust-chain fingerprints;
- issuance, expiry, and verification timestamps;
- current and previous source sequence where required;
- provenance, verification result, and revocation state;
- synthetic marker, policy version, and policy-controlled commit binding;
- bounded reason codes.

Commit binding is policy-controlled rather than universally optional:
independent-security-review and human-production-approval envelopes must carry
the exact assessed commit. Their typed payloads also carry the target commit,
and both values must match the acceptance context.

Canonical JSON and SHA-256 provide deterministic equality and lineage. A
fingerprint is not a signature. The envelope deliberately excludes credentials,
private keys, raw signed statements, raw broker responses, account identifiers,
and arbitrary exception text.

The model rejects naive timestamps, reversed windows, partial or skipped
sequence transitions, malformed fingerprints, contradictory synthetic claims,
and independently-verified claims lacking signed trust-chain metadata. The
offline evaluator then independently revalidates the exact model, subject,
environment, scope, policy, required release commit, chronology, freshness, signature
metadata, sequence requirement, and revocation state.

Even a structurally valid envelope claiming independent verification remains
blocked because Phase 21 has no independently configured verifier. This avoids
turning a model-shaped object into authority.

## Environment boundaries

| Environment | Permitted | Forbidden through Phase 21 | Promotion evidence |
|---|---|---|---|
| Local development | Contract inspection, documentation, offline reports | External access, execution, acceptance | None; remains local |
| Synthetic tests | Deterministic fixtures and adversarial validation | Real credentials, independent acceptance, execution | Test results only |
| Isolated integration sandbox | Future bounded provider connectivity under separate approval | Production acceptance and trading | Authenticated sandbox evidence plus independent review |
| Independently verified pre-production | Future external verification and rehearsal | Production execution | Independent evidence for every dependency and exact release |
| Production | Unavailable | Authorization, submission, and execution | Separate future governance decision; not implemented |

Synthetic success cannot promote a dependency. Sandbox success cannot establish
production acceptance. Changed environment, subject, scope, policy version,
commit, expiry, sequence, or revocation state invalidates applicable evidence.
Phase 21 implements no automatic promotion.

## Offline operator workflow

The safe workflow is:

1. Build the canonical registry and inspect its stable fingerprint.
2. Select an explicit environment, subject, scope, policy version, release
   commit, and evaluation time.
3. Load already-supplied sanitized envelopes; do not fetch external evidence.
4. Revalidate exact model structure, chronology, freshness, scope, release,
   sequence, and revocation state.
5. Classify missing, invalid, stale, revoked, duplicate, or conflicting items.
6. Mark valid fixtures `synthetic_tested` while retaining the
   `SYNTHETIC_EVIDENCE_NOT_OPERATIONAL` blocker.
7. Emit a deterministic, sanitized report containing every dependency exactly
   once and only stable blocker codes and fingerprints.

The workflow never reads secrets, calls providers, mutates trust or replay
state, acknowledges alerts, promotes an environment, deploys, or executes.

Before the workflow reads any context field, it requires the exact
`DependencyAcceptanceContext` type. It defensively copies the model's raw field
state without invoking instance serialization methods, requires the raw keys to
match the declared context fields exactly, rejects non-empty Pydantic extra
state, and strictly reconstructs the canonical model. This catches the
supported validation-bypassing copy cases, including missing or undeclared
fields, shadowed serialization methods, wrong types, malformed identifiers and
fingerprints, and naive timestamps. Invalid contexts raise a deterministic
validation error; they do not produce a partially trusted report or fall back
to caller data. This is a boundary for the documented Pydantic model state, not
a claim to defend against arbitrary code already executing inside the process.

Every supplied evidence envelope and its typed payload are also reconstructed
before the evaluator reads the dependency identity used for grouping. If any
object cannot be reconstructed, its identity is neither trusted nor guessed:
the complete offline report remains blocked, every dependency is classified
`invalid`, and the bounded blocker is `DEPENDENCY_EVIDENCE_MALFORMED`. This
prevents a mixed valid and unattributable-malformed set from silently dropping
the malformed object or assigning it to caller-selected policy state.

## Deterministic maturity precedence

General single-envelope failures are collected before one explicit maturity
decision. The precedence is `revoked` > `invalid` > `stale` > `unavailable` >
`local_contract`. Terminal revoked, superseded, or compromised identity state
therefore remains visible; malformed, mismatched, future-dated, or otherwise
invalid evidence cannot be softened to stale; stale evidence remains distinct
from an unavailable dependency. Synthetic evidence is labeled
`synthetic_tested` only when no failure maturity applies. Duplicate and
conflicting multi-envelope cases retain their separate early classifications.

Recovery evidence keeps its narrower established rule inside that general
boundary: terminal revocation wins; future or envelope-window-invalid recovery
timestamps beat staleness; an already invalid envelope remains invalid; an
otherwise valid old rehearsal remains stale; and only then do other recovery
payload defects classify as invalid. All applicable bounded reasons remain in
the report regardless of which maturity wins.

## Dependency-specific gate rules

Phase 21 uses discriminated, immutable payloads for local structural and policy
checks. These are sanitized acceptance summaries, not replacements for the
existing signed provider attestations. Their canonical fingerprints can link a
future authenticated attestation to the exact typed payload, but do not verify
its signature, issuer, trust chain, or real-world occurrence.

- **Broker payload:** exact account-scope fingerprint, source identity
  fingerprint, snapshot generation, complete required-field set, lineage,
  conflict count, and read-only capability metadata.
- **Reconciliation payload:** source identity, snapshot/generation, expected and
  covered records, completeness, discrepancies, and generation conflict.
- **Monitoring payload:** bounded signal/test identity, delivery outcome,
  receipt identity/fingerprint, event time, and destination fingerprint.
- **Recovery payload:** backup/rehearsal identity, start/completion, observed and
  maximum RPO/RTO, replay/witness consistency, outcome, and evidence fingerprint.
  Start and completion cannot be future-dated; completion must fall within the
  evidence envelope's inclusive issuance/expiry window. Completion must also be
  no more than the registry's 30-day recovery freshness limit before evaluation.
  The exact cutoff is accepted. Older completion is stale when it remains within
  its envelope; wrapping it in a newly issued envelope cannot refresh it and is
  instead an invalid window relationship. Future or window-invalid rehearsal
  timestamps are invalid even when the envelope is also stale. Revocation
  remains terminal and takes precedence over timestamp invalidity. At exact
  envelope expiry the completion is inside the inclusive recovery window, while
  the envelope itself is stale under the established expiry rule.
- **Security-review payload:** reviewer identity/role, independence claim,
  exact scope and commit, findings, unresolved severities/blockers, decision,
  expiry, and evidence fingerprint.
- **Human-approval payload:** approver identity/role, exact scope, commit and
  policy, decision window, revocation, decision identity/outcome, and evidence
  fingerprint.

Caller reason codes and claimed verifier results are never used as local policy
findings. The evaluator emits its own bounded codes. Structurally complete
payloads still retain explicit unverified-issuer, unverified-capability,
unverified-receipt, or unverified-independent-review blockers.

- **Trust root:** locally configured or self-declared roots are insufficient;
  independent provisioning, rotation, expiry, and revocation are mandatory.
- **Lifecycle:** unknown, stale, unavailable, revoked, superseded, compromised,
  or non-monotonic status blocks acceptance.
- **Witness:** confirmations must be authenticated, challenge-bound,
  stream-bound, monotonic, and independently administered. Unknown outcomes
  require reconciliation, never retry or acceptance.
- **Broker evidence:** source identity must be authenticated and read-only;
  evidence must be complete, fresh, and exact-scope. Phase 21 makes no broker
  query.
- **Reconciliation:** authoritative identity, snapshot/generation coverage, and
  zero unresolved discrepancies are required.
- **Monitoring:** local event emission is not delivery. An authenticated receipt
  and owned escalation path are required.
- **Recovery:** backup existence is insufficient. A restoration rehearsal,
  integrity proof, approved objectives, and replay/witness consistency are
  required.
- **Security review:** self-review is not independent review. The reviewer,
  exact scope, findings, remediation, and decision must be authenticated.
- **Human approval:** approval must bind role, release commit, scope, policy,
  expiry, and revocation. It cannot change execution controls in Phase 21.

## Observability and redaction

Phase 21 extends the existing `OperationalSignal` and
`OperationalReasonCode` enums rather than creating another free-form event
format. Signals cover dependency evidence, identity, sequence, invalidation,
alert delivery, recovery rehearsal, and production approval. Exact enum types
remain required by `operational_event()`.

Events contain bounded states and reason codes only. They must never include
raw statements, signatures, credentials, account identifiers, provider
responses, or arbitrary exception messages. Alert-delivery success cannot be
claimed without a real authenticated receipt; no monitoring connection exists
in this phase.

## Failure and recovery

- Missing or unavailable evidence remains unavailable.
- Malformed, incorrectly scoped, future-dated, or policy/commit-mismatched
  evidence is invalid.
- Expired evidence is stale.
- Multiple differing envelopes conflict; exact duplicates are rejected.
- Revoked, superseded, or compromised identity state is terminal for that
  assessment.
- Unknown witness or reconciliation outcomes remain blocked and require an
  external authoritative resolution.
- Local database restoration does not prove rollback safety. The independent
  witness must accept the restored checkpoint in a future authorized process.
- Recovery never silently changes a root, clears revocation, advances replay,
  creates approval, or enables execution.

## Test scope and residual risks

Synthetic tests cover completeness, determinism, immutability, caller-claim
rejection, all initial unavailable states, synthetic non-acceptance, identity
and environment mismatch, chronology, expiry, revocation, duplicate/conflict,
signature and sequence requirements, broker/reconciliation incompleteness,
manual prerequisites, adversarial registry equality, recovery chronology and
freshness boundaries, bounded events, and static absence of provider/network/
execution imports and wiring.

Tests prove local contract behavior only. Remaining external work is genuine
trust-root provisioning, authenticated lifecycle service, independent witness,
read-only broker evidence integration, authoritative reconciliation, monitoring
delivery, recovery rehearsal, independent review, and authenticated human
approval. Production authorization, submission, and execution remain false.
