# Phase 16 — Authenticated External Trust Integration Contracts

## Executive summary

Phase 16 establishes a strict, read-only integration boundary for external
root-provisioning, lifecycle, and broker-evidence attestations. It does not
connect to an external authority or broker. No independently authenticated
provider is configured, so every locally verifiable statement remains
`CONFIGURED_UNAUTHENTICATED` and non-authorizing.

Production authorization, submission, demo execution, and live execution all
remain structurally false.

## Verified trust hierarchy

1. Personal Ed25519 keys can be protected locally by Windows DPAPI. This is
   local custody, not independent authority.
2. `ConfiguredEd25519TrustAnchor` accepts only public verification material and
   fixes `independently_trusted` to false.
3. Phase 15 governance manifests bind anchor lifecycle and rotation to a
   separately configured root, but that root is not independently provisioned.
4. Phase 16 external attestations bind a provider, verification root, request,
   challenge, capability, subject generation/fingerprint, scope, policy,
   sequence, chronology, validity, and capability-specific state.
5. The external rollback-witness contract remains provider-neutral and
   unavailable by default. Local SQLite is not an external witness.
6. Broker evidence remains read-only and structurally validated but
   unauthenticated. Reconciliation is a separate requirement.
7. Explicit production authorization and submission-boundary review remain
   unavailable and cannot be produced by any diagnostic model.

## Implemented contracts

`app/models/authenticated_external_trust.py` defines immutable, versioned
contracts for:

- root-provisioning attestations;
- authoritative lifecycle-status attestations;
- broker-evidence provenance and reconciliation attestations;
- strict signed envelopes and non-authorizing assessments.

`app/services/authenticated_external_trust.py` defines a narrow read-only
provider protocol and a no-I/O unavailable default. The integration validates
Ed25519 signatures only against a separately supplied
`ConfiguredEd25519TrustAnchor`; a response cannot supply or replace that key.
It checks exact request/challenge binding, provider and root identity,
capability, subject identity/generation/fingerprint, scope, policy, monotonic
sequence, retrieval chronology, freshness, expiry, lifecycle, and
reconciliation state.

Provider exceptions are converted into generic unavailable results. Signed
payloads, native errors, credentials, and private material are not logged or
returned. Unknown schema versions and malformed signatures fail during model
construction.

Even after valid signature and binding verification, the result is
`CONFIGURED_UNAUTHENTICATED`, because the repository cannot prove that the
configured verification root was independently provisioned. All authority and
execution fields are `Literal[False]`.

## Provisioning protocol requirements

A future operational provisioning provider must independently bind:

- provider and verification-root identity, version, fingerprint, and Ed25519
  algorithm;
- governed root identity, generation, fingerprint, intended scope, and policy;
- request fingerprint and unpredictable challenge;
- monotonic source sequence;
- issuance, observation, retrieval, validity, and expiry chronology;
- active, revoked, superseded, compromised, or recovery-required lifecycle.

An operator must authenticate the provisioning provider and its verification
root out of band. A locally generated key, self-signed proof, configuration
value, or successful parser cannot perform that bootstrap.

## Lifecycle, rotation, revocation, and recovery

Phase 15 remains the canonical rotation validator. Its signed predecessor and
successor rules, half-open validity intervals, exact zero-overlap boundary, and
generation/sequence lineage are unchanged. Phase 16 adds a future retrieval
contract for current lifecycle status; it does not make cached manifests
authoritative.

Online freshness is required when policy requires current revocation status.
Provider unavailability, stale or replayed sequence, conflicting lifecycle,
revocation, compromise, supersession, and recovery-required state fail closed.
Local backups and legacy trust records cannot restore authority.

## External monotonic witness

Phase 15's `ReadOnlyExternalRollbackWitnessProvider` remains the witness
boundary. It binds authority, anchor generation/fingerprint, replay-store ID,
checkpoint fingerprint, request, challenge, sequence, and freshness. The
default provider reports unavailable, and its models cannot set provider
authentication, independent monotonicity, or external rollback assurance true.

A future witness must be independently administered, authenticated under an
out-of-band trust root, and maintain monotonic state outside the host. A local
counter, file, SQLite database, hash chain, or same-user process is not an
external witness. Coherent rollback of local state remains undetectable.

## Broker evidence and reconciliation

`ExecutionEvidenceRequest` and `TrustedExecutionEvidenceService` remain the
typed read-only snapshot boundary. Phase 16's broker attestation overlays an
exact evidence fingerprint and explicit reconciliation status without copying
raw provider payloads into agents.

Signature verification proves only that a configured key signed a statement;
it does not prove snapshot completeness or current broker truth. An operational
provider must separately guarantee authenticated provenance, complete account,
instrument, position and order scope, monotonic versioning, freshness, and a
defined reconciliation protocol. Unresolved reconciliation always blocks.

No Phase 16 code imports MT5, brokers, Pilot, FinalExecutionGate, adapters,
execution state, or submission APIs.

## Readiness semantics

The readiness report now distinguishes:

- governance-root provisioning;
- independent trust-anchor verification;
- lifecycle freshness;
- rotation/recovery assurance;
- durable local replay and atomic consumption;
- external rollback assurance;
- structural execution evidence;
- authenticated broker evidence;
- authoritative reconciliation;
- submission review and explicit execution authorization.

Configured-but-unauthenticated external statements are `BLOCKED`, never
`SATISFIED`. Missing providers are `UNAVAILABLE`; stale, invalid, revoked,
superseded, and conflicting results retain explicit failure states.

## Threat model and failure behavior

The boundary rejects evidence-supplied key substitution, altered signed
payloads, cross-provider/request/challenge/subject/scope/policy reuse, sequence
rollback, stale or future chronology, expired statements, terminal lifecycle
states, and unresolved reconciliation. Provider exceptions and malformed
responses fail closed with sanitized reason codes.

This does not defend against a privileged attacker who can replace application
code and trusted configuration together, compromise a genuine external
authority, or coherently roll back all local state. Those require operational
controls outside this repository.

## Tests

`tests/test_authenticated_external_trust.py` uses synthetic Ed25519 keys and
isolated fake providers to verify the negative contract. Fakes demonstrate
validation and failure behavior only; they never establish an operational
integration. Coverage includes key substitution, payload alteration, exact
lineage, sequence rollback, stale chronology, provider failure, revocation,
reconciliation conflict, model immutability/non-authority, readiness blockers,
and static execution/network isolation.

No real external integration tests exist because no authenticated authority,
witness, or broker-evidence provider is configured.

Validation in the managed session completed as follows:

- Phase 16 focused contracts: **14 passed**.
- Compatible Phase 14/15 trust and evidence regressions: **69 passed**, with
  three fixture-dependent cases deselected because the managed Windows token
  cannot access pytest's owner-ACL temporary root.
- Python compilation and `git diff --check`: passed.

The normal full-suite attempt was blocked before test execution by Windows
`WinError 5` on pytest's temporary root. This is an environment result, not a
passing full suite. Owner-session full-suite validation remains required before
the checkpoint is treated as fully regression-validated.

## Human and infrastructure blockers

1. Independently establish and approve the provisioning verification root.
2. Select an authenticated lifecycle/revocation authority and freshness policy.
3. Deploy an independently protected monotonic rollback witness.
4. Integrate authenticated, complete broker/account evidence without execution
   authority.
5. Define authoritative reconciliation for uncertain broker outcomes.
6. Complete independent security review and explicitly authorize the production
   submission boundary.

Until all blockers are operationally satisfied and independently reviewed,
production authorization is not permitted and demo/live submission remains
disabled.

## Phase 18 hardening addendum

Phase 18 preserves the Phase 16 non-authorizing boundary while closing three
software-only gaps:

- Broker attestations now sign and retain the exact validated-evidence request
  and snapshot fingerprints, source-scoped account fingerprint, observation and
  retrieval times, and matched reconciliation identity/fingerprint. Readiness
  recomputes this lineage from the exact evidence and reconciliation objects.
  A match is still `BLOCKED`, never `SATISFIED`, because no independent provider
  or root is operational.
- The existing Phase 14 SQLite schema is reused without migration. One
  `BEGIN IMMEDIATE` transaction consumes a challenge stream and the applicable
  provider/root/subject/scope/policy sequence stream, writes both claims and
  both history events, validates semantic consistency, and commits them
  together. Local durability still cannot detect coherent database rollback.
- Provider reads have a configurable two-second default and a thirty-second
  hard maximum. Calls use a process-wide budget of four daemon workers. Python
  cannot safely terminate a blocked thread, so late responses are discarded;
  exhausted capacity fails closed and no retry or cached success is attempted.

Structured logging also redacts secret-looking values in every top-level
identifier field, in addition to filtering custom fields. This is defensive
redaction, not permission to send secrets to logging APIs.
