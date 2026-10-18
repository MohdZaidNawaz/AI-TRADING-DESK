# Phase 13I personal signing and trust foundation

This is a non-authorizing integrity foundation for personal trading. It does
not designate an approver, create an independent trust anchor, authorize
production, or enable order submission.

## Canonical signing payload

The `personal-policy-approval-payload/v1` payload uses sorted-key, compact,
UTF-8 JSON with finite numeric values. It binds the unique approval ID, issue
and expiry times, signing-key ID, policy ID/version/fingerprint, and the full
immutable controlled-demo policy. The nested policy includes the exact broker
account/server identity, XAUUSD mapping, currency, order/filling modes, volume,
position, frequency, monetary-risk, stop-distance, notional, market-freshness,
and policy-validity constraints. Any alteration changes the payload
fingerprint and invalidates the detached signature.

Ed25519 is the only declared signature algorithm. An envelope binds the exact
approval ID, key ID, canonical payload fingerprint, and a strict 64-byte
signature. Verification also requires current trust metadata, available and
fresh revocation state, a non-expired active key, exact policy binding, and a
replay decision for the unique approval ID.

## Windows key-provider requirements

The current application has no production Windows key provider. The default
explicitly reports unavailable and never falls back to plaintext. A future
provider must implement creation, public-key export, signing, verification,
rotation, revocation, and recovery through reviewed Windows protection:

- Prefer a Windows CNG persisted asymmetric key whose private material is
  non-exportable when the provider and hardware genuinely enforce that policy.
- If DPAPI is used, encrypt private material for the current Windows user,
  authenticate metadata separately, restrict file ACLs, and document that
  DPAPI protection is not automatically hardware-backed or non-exportable.
- Never return, log, serialize, or persist private key material through domain
  models, audit events, exceptions, configuration, or source control.
- Identify public keys by a deterministic public-key fingerprint and explicit
  generation. Verify the exported public key before every signing operation.
- Make rotation explicit: a new generation links to the predecessor, the old
  key becomes inactive, and overlap is permitted only by an approved policy.
- Make compromise recovery fail closed. Restoring metadata is distinct from
  restoring signing capability; unavailable recovery must not create a new
  implicit authority.

Adding a package or implementing CNG/DPAPI requires separate dependency,
platform, key-custody, recovery, and security review. It must not be silently
activated by importing this module.

## Trust bootstrap and revocation

The user must independently verify the initial public-key fingerprint through
a governance process separate from routine signing. A local database row, key
file, hash, Git commit, test fixture, or successful self-signature proves only
local possession—not independent authorization.

An authoritative trust service must own current key status, key generations,
revocation and supersession events, freshness, and replay protection. Its
records require stable event IDs, monotonic sequence, durable audit provenance,
and an approved outage policy. Unknown keys, sequence rollback, stale or
unavailable revocation state, conflicting rotation, duplicate approval IDs, or
provider failure must block verification. Independent revocation authority is
not currently configured, so production remains blocked.

## Remaining production dependencies

Before any production review, FRIDAY still requires all of the following:

1. A real human governance decision naming the personal approver and scope.
2. Independent establishment of the public-key trust anchor and custody rules.
3. A reviewed Windows protected-key provider and tested compromise recovery.
4. Durable authoritative revocation, rotation, expiry, and replay state.
5. Trusted broker/account evidence and authoritative reconciliation.
6. Reviewed cross-store consumption/reservation and submission boundaries.
7. Explicit security, operational, incident-response, and production approval.

The Phase 13G readiness gate remains unchanged and fail-closed. Phase 13I is
not wired to execution, Pilot, Sentinel approval consumption, reservation
state, MT5, broker adapters, public APIs, or the frontend.
