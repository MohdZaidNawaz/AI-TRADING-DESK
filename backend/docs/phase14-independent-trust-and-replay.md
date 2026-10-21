# Phase 14 — Independent Trust and Durable Replay Safeguards

Date: 2026-10-21

Baseline: `675335be73abb220c6ce7c1188090ef39f626bfd`

## Executive summary

Phase 14 adds an explicitly initialized, schema-verified SQLite replay store for
signed independent key-status statements. Replay identity consumption is atomic
across processes sharing the database and survives ordinary process restart.
The verification envelope still cannot supply its own verification key, and
every verification result remains structurally non-authorizing.

This phase does **not** establish an independent trust authority. The configured
Ed25519 key is still local configuration with `independently_trusted=false`.
There is no independently authenticated anchor provisioning, rotation,
revocation, monotonic witness, or external rollback detector. Those conditions
remain explicit production-readiness blockers.

Production authorization, submission, and demo/live execution remain disabled.

## Trust boundaries

### Implemented local boundary

- `ConfiguredEd25519TrustAnchor` accepts only a valid Ed25519 SubjectPublicKeyInfo
  public key and is structurally unable to claim independent trust.
- `SignedIndependentKeyStatusStatement` contains no verification key.
- `IndependentKeyStatusVerifier` compares authority, anchor ID/version/fingerprint,
  subject key/generation, exact scope, request fingerprint, challenge, sequence,
  chronology, freshness, and signature before replay consumption.
- `DurableIndependentReplayStore` consumes the canonical replay binding in a
  local SQLite transaction and returns no accepted result until commit succeeds.
- Readiness reports local replay persistence and atomicity separately from
  unavailable external rollback assurance.

### Unavailable independent boundary

No repository component can independently prove who provisioned the configured
anchor. A future source must be authenticated outside the authority it verifies,
govern anchor purpose/scope/version/rotation/revocation, and supply a trustworthy
monotonic reference. A model-shaped object, local DPAPI state, SQLite record,
local signature, or personal ceremony cannot satisfy this boundary.

## Threat model and disposition

| Threat | Existing or Phase 14 defense | Residual gap / classification |
| --- | --- | --- |
| Evidence supplies its own key | Envelope has no public-key field; verifier uses separately configured anchor | Anchor provisioning is local and not independently authenticated; high external blocker |
| Anchor substitution | Exact anchor ID/version/fingerprint are request- and signature-bound | A privileged local actor can replace configuration and request together; external governance required |
| Anchor downgrade/rollback | Exact version/fingerprint mismatch fails | No independent monotonic anchor catalog; rollback resistance unavailable |
| Revoked authority signs again | Signed terminal key states fail closed | Authoritative revocation availability is external and absent |
| Stale/expired/cross-scope evidence | Exact binding, UTC chronology, freshness and expiry validation | Depends on authenticated authority evidence and trustworthy operational time |
| Signed request replay | Canonical replay binding is consumed once | Local database rollback can restore older state |
| Concurrent duplicate requests | `BEGIN IMMEDIATE`, primary/unique constraints and one commit linearization point | Applies only to processes sharing this database |
| Crash before commit | Transaction rolls back; no accepted result is returned | Storage/platform guarantees remain SQLite and OS dependent |
| Crash after commit before response | Caller receives unavailable/uncertain; persisted claim blocks retry | External workflow must reconcile uncertainty rather than retry |
| Missing/locked/corrupt storage | No auto-create during verification; unavailable result or construction failure | Operator intervention is required |
| Locally inconsistent replay state | Physical, schema, foreign-key, and semantic claim/head/history checks fail closed without repair | A coherent older snapshot remains locally indistinguishable from valid older state |
| Restored database snapshot | Semantic checks detect internally contradictory claims, heads, or events | A coherent full snapshot rollback is not detectable locally; external witness required |
| Same-user/admin tampering | Detectable physical, schema, foreign-key, and semantic damage fails closed | Privileged local replacement with coherent state is outside the local guarantee |
| Forged identifiers/sequences | Canonical strict models, signed payload and stream-head monotonicity | Producer protocol and anchor must be independently governed |
| Rotation leaves old key valid | Request and statement bind exact anchor version/fingerprint | No authenticated rotation manifest or monotonic latest-version resolver exists |
| Migration resets replay | Only exact v1 schema upgrades to v2 transactionally; claims are preserved | No legacy anchor is elevated; unsupported/partial schemas fail closed |
| Logs/readiness overstate assurance | Diagnostics contain safe IDs/fingerprints and literal false authority flags | Operational logs still require independent retention and review |

## Replay identity

The binding fingerprint covers:

- authority identity;
- anchor identity, version, and fingerprint;
- subject key identity and generation;
- exact scope fingerprint;
- signed source sequence;
- signed statement identity and statement fingerprint.

The stream fingerprint excludes the individual statement and sequence while
retaining authority, anchor, subject key/generation, and scope. It provides one
monotonic sequence head for that exact stream. This prevents identities from
being shared across authorities, anchor versions, keys, generations, or scopes.

## Transaction and crash guarantees

`check_and_record()` opens an existing database, enables foreign keys, starts
`BEGIN IMMEDIATE`, validates physical integrity, schema/migration state, foreign
keys, and semantic consistency, checks the exact claim and stream head, inserts
the claim, advances the head, inserts an immutable deterministic replay event,
revalidates semantic consistency, and commits.

The commit is the linearization point. An accepted result is constructed before
commit but is returned only after commit succeeds. A failure before commit rolls
back and reports unavailable. If commit succeeds but acknowledgement fails, the
caller receives unavailable and a later request sees a duplicate; it cannot
obtain a second acceptance.

SQLite uses `synchronous=FULL`, database constraints, and a write transaction.
There is no in-memory fallback. Lock or storage failure fails closed.

## Schema, migration, and recovery

- Missing databases are never created by the verifier/store constructor.
- `initialize()` is the only explicit bootstrap operation.
- Version 2 is the current schema.
- An exact version-1 schema may be upgraded transactionally to version 2.
- Before migration, version-1 claims must have canonical binding and stream
  fingerprints and exactly one maximum-sequence head per retained stream.
- Migration preserves all valid replay claims and heads and derives one
  deterministic history event for every retained claim.
- Unknown versions, incomplete migration markers, missing objects, weakened DDL,
  physical corruption, foreign-key failures, or semantic contradictions are
  rejected without repair.
- A failed migration transaction leaves the exact version-1 state intact.
- No migration imports or elevates trust anchors.

Semantic validation recomputes each retained claim and stream fingerprint,
requires every stream head to reference that stream's maximum-sequence claim,
and requires a deterministic one-to-one claim/history relationship with matching
timestamps. Missing, stale, wrong-stream, orphaned, or contradictory state is
unavailable; initialization and verification never recreate or discard it.

Recovery from missing, corrupt, or rolled-back state is an operator/security
decision. The application must remain unavailable for protected verification;
it must not create an empty store and continue.

## Rollback limitations

The store protects against ordinary restart, duplicate delivery, concurrent
consumption, sequence rollback visible within the current database, and
internally inconsistent claims, heads, or history. It does not protect against
an administrator or compromised same-user process restoring a coherent older
snapshot of the database and its metadata. A local store ID, counter, sidecar
file, DPAPI blob, or local signature would be rollable with the same host and is
therefore not represented as independent rollback protection.

Production readiness continues to report `external_rollback_assurance` as
unavailable. A separately protected monotonic service, append-only witness, or
hardware-backed counter is required to change that status.

## Readiness implications

Readiness now distinguishes:

- local replay-store availability;
- atomic replay consumption;
- external rollback assurance;
- personal trust bootstrap;
- independent trust-anchor provisioning;
- authoritative key status/revocation;
- broker evidence and reconciliation;
- production authorization and execution mode.

A valid local replay store may satisfy only the first two diagnostics. It cannot
satisfy independent governance, rollback assurance, or production authorization.
All authorization/submission/execution flags remain literal `false`.

## Test matrix

Deterministic tests cover explicit bootstrap, missing/corrupt/unsupported/partial
schemas, weakened schema rejection, missing/stale/wrong-stream heads, fabricated
stream identities, missing/orphaned/inconsistent history, semantic validation
during consumption, transactional v1-to-v2 migration, inconsistent legacy-state
rejection, failed migration preservation, exact duplicates, precise integrity-
error classification, transaction rollback, sequence conflict/rollback,
independent streams, restart persistence, concurrent consumers, lock failure,
failure before commit, uncertainty after commit, verifier integration, fake
readiness injection, and non-authorizing diagnostics.

Validation on the final implementation tree completed as follows:

- Phase 14 security-focused regressions: **210 passed**.
- Complete backend suite: **1059 passed**, zero failures or skips.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed.

No frontend contract changed, so frontend lint/build were intentionally not
repeated for this backend-only security milestone.

## External dependencies and residual risk

The following remain blocked by external governance or infrastructure:

1. Independent anchor provisioning and authenticated provenance.
2. Authority/anchor lifecycle policy, rotation, suspension, revocation, and
   recovery governed outside the subject key and local process.
3. An independently protected monotonic replay/rollback witness.
4. Authenticated broker/account evidence and authoritative reconciliation.
5. Independent production submission-boundary security review and explicit
   human authorization.

Passing local tests does not establish any of these controls and does not permit
production authorization or trading.
