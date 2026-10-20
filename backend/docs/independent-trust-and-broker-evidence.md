# Independent trust and broker-evidence boundary

This milestone adds verification capability, not production authority.

## Signed key-status evidence

`IndependentKeyStatusVerifier` verifies a versioned Ed25519 signature over a
canonical key-status statement. The signed payload binds the authority and
trust-anchor identities, anchor version and fingerprint, request fingerprint,
challenge, subject key and generation, exact account/instrument policy scope,
source sequence, lifecycle state, and time window. The statement contains no
verification key. A public key must be configured separately as a
`ConfiguredEd25519TrustAnchor`.

The configured key is still only local configuration. Constructing an anchor,
verifying a signature, or implementing a replay protocol does not demonstrate
that an independent operator established or governs that key. Every result is
therefore structurally unable to authorize production, submission, or
execution. Revoked, superseded, compromised, and recovery-required statements
remain terminal fail-closed evidence.

The replay binding includes the authority, anchor identity/version/fingerprint,
subject key and generation, scope fingerprint, source sequence, statement ID,
and statement fingerprint. The default replay protector is unavailable. No
local in-memory or SQLite component is described as externally durable replay
or rollback protection.

## Read-only broker evidence

The existing `ExecutionEvidenceRequest`, `ProviderExecutionEvidence`, and
`ValidatedExecutionEvidence` contracts bind provider identity, account,
canonical and broker symbols, instrument constraints, quote time and freshness,
complete position and pending-order tuples, portfolio-risk input lineage, and
source provenance. `BrokerReconciliationRequest`, observation, and assessment
contracts separately represent reconciliation state. They are read-only and
non-authorizing.

Those contracts validate shape, lineage, completeness, and freshness; they do
not authenticate an external provider. Consequently, production readiness now
reports fresh structurally valid broker evidence as
`TRUSTED_EXECUTION_EVIDENCE_PROVIDER_UNAUTHENTICATED`, rather than satisfied.
An operational integration must authenticate a configured source independently,
bind an attestation to the exact evidence fingerprint and request, provide
externally durable replay protection, and implement authoritative reconciliation
before this prerequisite can become satisfiable.

## Still unavailable

- an independently governed and reviewed trust-anchor configuration;
- an authenticated external key-status/revocation/recovery service;
- externally durable replay and rollback protection;
- an authenticated, trusted broker/account evidence source;
- authoritative broker-state reconciliation; and
- production submission-boundary authorization and review.

No code in this milestone initializes MT5, contacts a broker, consumes an
approval, mutates execution state, or submits an order.
