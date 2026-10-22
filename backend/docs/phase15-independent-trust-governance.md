# Phase 15 — Independent Trust Governance and External Rollback Assurance

Date: 2026-10-22

Baseline: `cb53a8c1e2fb1f89fe2272c3efc0d04c4bb7ac39`

## Security outcome

Phase 15 adds strict, provider-neutral contracts for trust-anchor lifecycle
governance and an external rollback witness. It does **not** configure either
external dependency. Cryptographically valid local test evidence remains
non-authoritative, production authorization remains blocked, and submission and
execution remain disabled.

## Verified starting architecture

- `ConfiguredEd25519TrustAnchor` supplies a separately configured verification
  key and fixes `independently_trusted` to `false`.
- `IndependentKeyStatusVerifier` binds signed key status to authority, anchor,
  subject key/generation, scope, request, challenge, chronology, freshness, and
  replay result.
- `DurableIndependentReplayStore` provides local SQLite durability, semantic
  validation, atomic consumption, migration, and restart safety. It cannot
  detect a coherent rollback of the complete database.
- Personal bootstrap, local DPAPI custody, local SQLite state, and local
  signatures explicitly do not establish independent governance.

## Trust-anchor governance contract

`TrustAnchorGovernanceManifest` is a canonical signed manifest that binds:

- the separately configured governance root identity, version, and fingerprint;
- the governed authority and anchor identity, version, generation, and fingerprint;
- the exact account/instrument/policy scope and policy version;
- request fingerprint and challenge;
- source sequence, validity interval, lifecycle, and rotation lineage.

`IndependentTrustGovernanceVerifier` verifies the signature only with the
separately configured governance-root key. The signed envelope contains no key.
It rejects substituted roots, altered payloads, unknown schema versions,
downgrades, stale or expired manifests, missing rotation lineage, unauthorized
rotation, generation gaps, sequence rollback, and conflicting scope or policy.

The configured governance root is still local configuration with no independent
provenance. Consequently, a fully valid manifest produces
`CONFIGURED_UNAUTHENTICATED`, never
`VALID_INDEPENDENTLY_AUTHENTICATED`. The result models structurally reject a
claim of independent authentication in this phase. Revoked, expired, and
rotation-incomplete manifests remain distinct fail-closed states.

## Rotation, revocation, and recovery

- Generation one cannot name a previous manifest.
- Every later generation requires the exact previous manifest fingerprint,
  consecutive generation, increasing source sequence, identical governance and
  scope lineage, a signed `ROTATION_PENDING` predecessor, and an exact declared
  replacement target.
- Revoked anchors remain terminal diagnostic evidence and are never accepted
  through a fallback.
- Missing or contradictory governance material is invalid or unavailable; it
  never resets trust or local replay state.
- Recovery is an external governance ceremony. An operator must obtain the
  current authenticated manifest and revocation state from the independently
  administered authority, verify it out of band, resolve any incomplete
  rotation, and only then provision a reviewed integration. Local records must
  not recreate or reactivate trust.

## External rollback witness

`ExternalRollbackWitnessRequest` binds the expected provider, authority and
trust generation, anchor fingerprint, local replay-store identity, exact replay
checkpoint fingerprint, request, challenge, and freshness policy.
`ExternalRollbackWitnessIntegration` validates structural lineage and chronology
from a read-only provider. The default provider reports unavailable.

No authenticated monotonic witness exists in the repository. An injected test
provider can produce only `STRUCTURALLY_VALIDATED` evidence with
`provider_authenticated=false`, `independent_monotonicity_established=false`,
and `external_rollback_assurance=false`. A provider-supplied boolean cannot
change those literal values.

A future witness must be independently administered and authenticated, persist
an append-only monotonic sequence outside the FRIDAY host, bind the exact trust
generation and replay checkpoint, reject rollback and equivocation, survive
FRIDAY database restoration, and expose conservative outage/recovery semantics.
A local counter, file, SQLite table, DPAPI blob, same-user process, or hash chain
beside the replay database is not an external witness.

The manifest verifier's minimum sequence is request-bound but is not itself an
independently durable latest-generation registry. A caller that rolls back the
request, manifest, configured root, and all local state coherently cannot be
detected by this local composition. The missing external governance catalog and
monotonic witness therefore remain security blockers rather than implied
guarantees.

## Readiness implications

`ProductionAuthorizationReadinessGate` now reports local key custody and anchor
lifecycle as distinct prerequisites in addition to personal bootstrap,
independent anchor authentication, authority verification, local durable replay,
atomic consumption, and external rollback assurance. Phase 15 governance and
witness assessments can refine failure classification and provenance but cannot
satisfy either external prerequisite.

Every readiness report remains `blocked`. `production_authorization_available`,
`submission_allowed`, `execution_authority`, and each individual check's
`authorizes_execution` value remain literal `false`.

## Threats and tests

Deterministic synthetic-key tests cover unconfigured governance, valid local
signatures that remain unauthenticated, key substitution, altered lineage,
expiry, revocation, sequence downgrade, unauthorized and incomplete rotation,
unknown schema versions, fabricated independent status, unavailable witness,
provider-authentication fabrication, stale witness evidence, wrong trust
generation, wrong replay checkpoint, readiness blocking, and absence of
execution/network reachability.

Tests use isolated temporary SQLite storage and synthetic Ed25519 keys only.
They do not contact an authority, MT5, a broker, an account, or an execution
adapter.

Validation on the implementation tree completed as follows:

- Phase 15 focused tests: **15 passed**.
- Trust/governance/replay/readiness/recovery regressions: **163 passed**.
- Complete backend suite: **1074 passed**, zero failures or skips.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed.

## Remaining external blockers

1. Independent provisioning and authentication of the governance-root key.
2. An independently administered anchor manifest, rotation, revocation, and
   recovery service with durable replay protection.
3. An authenticated external monotonic rollback witness.
4. Authenticated broker/account evidence and authoritative reconciliation.
5. Independent production submission-boundary review and explicit human
   production authorization.

These dependencies cannot be manufactured by repository code or local tests.
Phase 15 does not permit production, demo, or live trading.
