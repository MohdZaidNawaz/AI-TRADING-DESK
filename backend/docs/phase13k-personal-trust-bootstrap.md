# Phase 13K personal trust bootstrap, revocation, and recovery

Phase 13K defines a fail-closed personal trust ceremony and diagnostic
boundaries for key status, replay protection, and recovery. It does not create
an independent authority and does not enable execution.

## Trust model and manual ceremony

A locally generated Ed25519 key and its DPAPI-protected private bytes establish
local custody only. The bootstrap ceremony presents the exact public-key ID,
generation, SHA-256 fingerprint, validity, personal scope, intended authority,
and declared trust-anchor identity, version, and fingerprint. The owner must
compare that fingerprint using a genuinely separate, trusted channel and record
an explicit decision bound to the complete ceremony, scope, trust-anchor, and
validity-window fingerprints. Decisions before presentation, at or after
expiry, or after the explicitly supplied evaluation time fail closed. The
evaluation time is deterministic input, not an independent time authority.

The software cannot perform that human comparison. A missing decision remains
`PENDING_HUMAN_CONFIRMATION`. A local key signature, a self-authored manifest,
or trust-on-first-use is rejected as a substitute. Even a correctly recorded
local confirmation has `independent_trust_established = false`; it is evidence
of the owner's local decision, not an independently verified trust anchor.

## Key-status and replay boundary

`PersonalKeyAuthorityIntegration` defines the read-only shape of a future
personal-key status authority. Requests bind an expected authority identity,
trust-anchor identity, version, and fingerprint, exact key identity and
generation, public-key and complete scope fingerprints, challenge, freshness
bound, and prior source sequence. Assessments retain those complete request
facts plus observation freshness and lifecycle lineage. Bootstrap and recovery
composition independently compare them with the current ceremony, local key,
scope, and original request; another subject's assessment cannot classify the
current key.
Sanitized observations add source authentication and trust-anchor validation
claims, current key state, expiry, stable event identity, monotonic sequence,
audit provenance, and explicit replacement lineage for supersession.

The integration rejects missing, mismatched, stale, expired, unauthenticated,
replayed, rolled-back, conflicting, revoked, compromised, and superseded
observations. The default provider and replay protector report unavailable.
Test doubles can demonstrate structural validation and failure behavior only;
their output remains non-authoritative and cannot satisfy production readiness.
No local row, local signature, DPAPI record, file lock, or system clock is an
independent revocation source.

A real deployment still requires independently authenticated transport or
offline verification, a separately established trust anchor, a durable replay
window, monotonic sequence governance, and independently operated revocation,
rotation, compromise, and outage procedures. Those are external blockers.

## Recovery behavior

Recovery is diagnostic and never mutates key state. Lost, corrupt or
undecryptable, compromised, revoked, superseded, interrupted, rolled-back, and
authority-unavailable states remain distinct. Every result fixes automatic key
replacement, revoked-key reactivation, production authorization, submission,
and execution authority to `false`.

- Missing or unreadable keys are never silently regenerated.
- Revoked keys are never reactivated.
- Superseded keys require out-of-band verification of the replacement.
- Compromise or rollback requires a new trust decision and chain.
- Interrupted rotation requires explicit lifecycle reconciliation.
- Missing authority or replay state blocks use rather than restoring an older
  local state.

Local DPAPI lifecycle records are not rollback-resistant. A user with access
can restore older files, so an external monotonic authority is still required.

## Readiness and execution boundary

The Phase 13G report now includes explicit personal bootstrap, authoritative
key-status, and replay/rollback-protection prerequisites. A local confirmation
is reported as local-only and blocked. Key authority and durable replay checks
remain unavailable because no genuine provider is configured. The report is
still structurally unable to authorize production or submission.

Phase 13K is not composed into Sentinel, Pilot, `FinalExecutionGate`, execution
state, reservations, MT5, broker adapters, public APIs, or the frontend. Live
and controlled-demo submission remain disabled.
