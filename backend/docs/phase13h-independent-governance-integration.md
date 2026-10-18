# Phase 13H independent governance integration boundary

This document specifies external requirements. It is not an approval, trust
anchor, production configuration, or execution authorization. Repository test
providers are synthetic and must never be accepted as production authority.

## Independent policy authority

A production provider must be independently operated and authenticated. For
each read it must return the current governance decision and approval lifecycle
for the exact policy ID, version, canonical fingerprint, account, execution
mode, symbol, and risk currency. The response must echo a single-use challenge
and request fingerprint, carry an immutable provider event ID, monotonically
ordered source sequence, observation and retrieval times, and a durable audit
reference. Current validity, expiry, revocation, and supersession must be read
from the authority at evaluation time rather than inferred from local records.

The trust anchor must be established outside the application, identify its
owner and approved scope, have explicit validity, and expose only public
identity material. Rotation requires a new generation linked to the previous
public fingerprint; overlap, retirement, compromise, and emergency revocation
rules are organizational decisions. Private keys, service credentials, and raw
provider payloads must never enter these contracts or logs.

A production implementation additionally needs authenticated transport or
offline signature verification, a durable replay window keyed by source event
and sequence, independently reviewed key custody, outage and degraded-mode
policy, and at-least-once audit projection deduplicated by stable event ID. An
outage, conflict, stale response, missing revocation state, unknown key,
sequence rollback, or ambiguous rotation must fail closed.

## Broker/account evidence and reconciliation

A production read-only broker evidence provider must independently obtain the
exact account/server identity, explicit canonical-to-broker symbol mapping,
instrument constraints, quote/source time, positions, pending orders, account
state, and the broker order/position state for the exact execution identity.
The observation must contain sanitized typed facts and provenance only. It must
not expose credentials or raw terminal/broker payloads, and it must not provide
submission, modification, cancellation, or close operations.

Authoritative reconciliation requires a durable, provider-authenticated stream
or query boundary with stable source event IDs and monotonic sequence, bounded
freshness, gap detection, and an explicit distinction between a confirmed
absence and provider unavailability. Account, server, order ID, symbol,
quantity, direction, prices, stop, target, status, and lineage must match the
immutable execution record. Any uncertain, conflicting, stale, or incomplete
state requires reconciliation and must never cause automatic retry or a
replacement reservation.

## Integration and transaction limits

The Phase 13H code validates shapes, exact bindings, freshness, and sanitized
provenance. It deliberately returns non-authorizing assessments and is not
wired to existing lifecycle owners. A future reviewed composition must still
resolve races between authority revocation, authorization consumption,
reservation commit, broker observation, and audit delivery. Separate external
systems cannot share the local SQLite transaction, so ambiguity after any
cross-system boundary must persist as `RECONCILIATION_REQUIRED` until an
independent broker observation resolves it.

Production review must reject startup unless all of the following are genuine
and independently verified:

- named organizational approver and approved policy scope;
- trust-anchor ownership, public identity, rotation, revocation, and outage rules;
- authoritative policy approval and revocation provider;
- trusted broker/account evidence provider and freshness policy;
- authoritative reconciliation provider with durable replay protection;
- reviewed durable cutover/consumption protocol and submission boundary;
- security, operational, incident-response, and production change approvals.

No item may be satisfied by a local row, hash, Git commit, test fixture,
protocol-shaped object, or this document. Phase 13G therefore remains blocked.
