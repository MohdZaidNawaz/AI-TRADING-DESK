# Phase 22: independent evidence verification architecture

## Boundary and threat model

Phase 22 is a strictly offline, non-authorizing verification foundation. It
accepts already-supplied Phase 21 evidence, an explicit immutable verification
configuration, and a fixed evaluation time. It performs no provider, network,
broker, MT5, readiness, submission, or execution operation.

The boundary distinguishes:

1. Pydantic structural validity and canonical payload integrity.
2. Exact context, scope, policy, environment, challenge, request, and release
   binding.
3. Ed25519 signature validity against a separately configured public key.
4. Authentication to that configured key identity.
5. Lifecycle and revocation claims that remain independently unverified.
6. Durable replay and rollback assurance, which remains unavailable.

An evidence-supplied public key, issuer label, signature fingerprint,
provenance label, verification result, or reason code cannot establish trust.
The configured key model is explicitly synthetic and test-only, forbids the
production environment, and fixes independent trust to false.

## Cryptographic contract

The implementation uses PyCA `cryptography` Ed25519, already present in the
backend dependency set. There is no algorithm negotiation or fallback.
Malformed keys, non-Ed25519 keys, malformed signatures, unsupported algorithm
identifiers, and invalid signatures fail closed.

The canonical ASCII JSON signature payload is domain-separated and binds:

- algorithm, key ID, key generation, root version and root fingerprint;
- Phase 22 verification-policy version;
- exact issuer, dependency ID and evidence type;
- the canonical Phase 21 envelope fingerprint and complete envelope content;
- subject, request, challenge, environment, scope, Phase 21 policy, target
  commit where applicable, timestamps, revocation claim, and typed payload.

Canonical SHA-256 fingerprints prove deterministic equality only. Ed25519
verification proves that the configured key signed those exact bytes. Neither
operation proves that the key was independently provisioned or governed.
Canonicalization operates on the fully validated Pydantic model, after its
documented identifier, enumeration, and UTC timestamp normalization. It does
not preserve or authenticate the original transport encoding, field order, or
equivalent pre-validation representation.

## Explicit trust configuration

`EvidenceTrustConfiguration` has no default instance. It must be supplied
explicitly and binds the configured Ed25519 anchor to one issuer, key identity,
generation, dependency set, non-production environment set, and scope set. The
configuration is reconstructed at the verifier boundary from an exact-class,
defensive copy of its raw declared field state. The nested trust anchor is
reconstructed the same way. Instance serializers are never invoked, and
subclasses, missing or undeclared fields, non-empty extra state, malformed
copied models, and malformed nested anchors are not accepted as ambient trust.

The configuration is deliberately `synthetic_test_only=True`,
`independently_trusted=False`, and
`lifecycle_independently_verified=False`. Active lifecycle is therefore a local
configuration claim, not authoritative revocation evidence. Revoked,
superseded, compromised, unknown, expired, and mismatched configurations block
verification.

A public key submitted with evidence is parsed only to reject malformed or
private material. It is never selected as the verification key and is reported
as untrusted metadata.

## Evaluation and replay behavior

The service revalidates exact request, context, signed-envelope, evidence, and
typed-payload models from defensive raw-state copies before evaluation or
signature verification. It does not dispatch through caller-shadowable model
serializers. It requires the service
evaluation time to equal the context evaluation time and checks the canonical
Phase 21 registry for evidence type, commit-binding, and freshness policy.
Future, expired, stale, revoked, superseded, compromised, unknown, mismatched,
and malformed evidence fails closed.

Failure precedence is deterministic. Exact binding failures produce
`binding_invalid`; otherwise future, expired, or stale evidence produces
`stale`; otherwise revoked, superseded, or compromised evidence claims produce
`revoked`, while an unknown lifecycle claim produces `unavailable`. Reports
retain every already-computed freshness and lifecycle status and bounded reason
even when an earlier category determines the disposition. Evidence lifecycle
values remain signed caller claims, not independently authenticated revocation.

Evidence/context binding and configured-root state remain distinct. Issuer,
key, generation, permission, environment, or scope configuration mismatches set
`binding_status=invalid`. A root that is not yet valid, has expired, or has a
non-active lifecycle does not retroactively invalidate an already valid
evidence/context binding: it preserves `binding_status=valid` and fails through
the disposition, lifecycle status, issuer status, and bounded trust-root reason.

Configured trust-root validity begins inclusively at `valid_from` and ends
exclusively at `expires_at`. Evidence expiry is also exclusive: evidence is
expired when `expires_at <= evaluated_at`. Registry freshness is inclusive at
the maximum age: `evaluated_at - issued_at == freshness_seconds` is fresh, and
any greater age is stale. UTC-normalized datetime precision, including
microseconds, is retained for these comparisons.

The request may supply previously seen evidence fingerprints for deterministic
duplicate evaluation. This is pure input evaluation and mutates no store. Every
successful configured-key verification still reports
`EXTERNAL_REPLAY_PROTECTION_UNAVAILABLE`; it is not durable replay or rollback
protection.

## Report semantics

The immutable report separately records structural, signature, configured-key
issuer, binding, lifecycle, freshness, and supplied-set replay status. A valid
synthetic signature may produce
`configured_key_verified_non_authoritative`, but the report always retains:

- `independent_verification_available = false`;
- `independently_verified = false`;
- `operationally_accepted = false`;
- `production_authorization_available = false`;
- `submission_allowed = false`;
- `execution_authority = false`;
- `authorizes_execution = false`.

Reason codes are a bounded enum. Reports retain fingerprints and statuses, not
private keys, credentials, raw broker responses, or arbitrary exception text.

## Test isolation and remaining blockers

Private-key generation exists only in isolated tests. Production code contains
public-key verification only and never generates or persists trust material.
Tests cover real Ed25519 verification, tampering, malformed encodings and key
types, unconfigured and embedded-key trust attempts, key identity/generation/
root rotation, exact context and release binding, lifecycle, freshness,
duplicate evaluation, deterministic reports, and static runtime isolation.

Phase 22 does not supply independently provisioned roots, authoritative
lifecycle/revocation, an external monotonic witness, authenticated broker
evidence, authoritative reconciliation, verified monitoring delivery, recovery
rehearsal, independent security review, or authenticated human production
approval. Production authorization and all trading execution remain disabled.
These checks harden malformed in-process model boundaries; they do not protect
against arbitrary malicious code already executing inside the process.
