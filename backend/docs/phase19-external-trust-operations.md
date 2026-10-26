# Phase 19 external trust operations

Phase 19 completes the safe local contract and validation work for external
trust operations. It does **not** configure an external service, independently
provision a root, authenticate a transport, or authorize execution.

## Trust boundaries

```text
independent bootstrap ceremony (external and unavailable)
        |
configured Ed25519 verification root (local, independently_trusted=false)
        |
bounded provider protocol (interface only; no network client)
        |
signed root | lifecycle | witness | broker | reconciliation statements
        |
signature + exact lineage + freshness + Phase 18 atomic replay consumption
        |
Phase 19 operational composition (diagnostic, always non-authorizing)
        |
production readiness (BLOCKED)
```

Transport identity, statement-signing identity, cryptographic validity,
independent provenance, lifecycle currency, rollback resistance, broker
evidence, reconciliation, authorization, readiness, and execution are separate
facts. None is inferred from another.

## Root provisioning and lifecycle

Root provisioning and lifecycle reuse the versioned Phase 16 attestation and
Phase 18 replay boundary. The signed subject includes the exact root identity,
generation, fingerprint, scope, policy, request, challenge, validity interval,
and source sequence. Ed25519 is the only supported statement algorithm.

The verification key is supplied to the integration independently of the
provider response. Provider-supplied keys cannot replace it. Nevertheless,
local configuration and a valid signature do not prove that a second-channel
bootstrap ceremony occurred. Therefore `independently_authenticated`,
`independent_root_provisioned`, and `authoritative_lifecycle_available` remain
literal false. Revoked, compromised, superseded, recovery-required, stale, or
unavailable lifecycle statements fail closed and are never replaced by cached
success.

Operational acceptance requires an independently administered provisioning
process that authenticates the root identity, public-key fingerprint,
algorithm, scope, policy, validity, rotation, revocation, and recovery process.

## Witness protocol and crash consistency

`RollbackWitnessLineage` binds:

- replay store and stream identities;
- local sequence head and durable history checkpoint;
- previous checkpoint, when present;
- independent witness sequence;
- an idempotency key; and
- `confirmed`, `unknown`, or `conflict` update outcome.

The lineage fingerprint is the signed attestation subject. The signed source
sequence must equal the witness sequence, so Phase 18's SQLite transaction
durably consumes its challenge and sequence claim. A duplicate or lower
sequence is rejected across restart and concurrent store instances.

No local transaction is held across an external call. A future operational
protocol must use the following recovery rules:

| Cut point | Required disposition |
| --- | --- |
| Local claim commits, witness update fails | Block; reconcile before another claim |
| Witness confirms, local completion uncertain | Retry only with the same idempotency key; verify before progressing |
| Witness outcome unknown | `RECONCILIATION_REQUIRED`; no replacement or automatic retry |
| Witness checkpoint lower or conflicting | Block as rollback/equivocation |
| Witness unavailable | Remain blocked; never use cached success beyond validity |
| Older local database restored | External witness must reject the lower checkpoint |

The repository supplies contracts and deterministic fakes only. It has no
independently administered witness, so `external_rollback_assurance` remains
literal false. Local SQLite cannot detect coherent restoration of itself.

## Broker evidence and reconciliation

Broker authentication retains Phase 18's exact evidence lineage: provider,
request, immutable evidence fingerprint, source-scoped account, observation and
retrieval times, and matched local reconciliation fingerprint.

`AuthoritativeReconciliationLineage` additionally binds the account, scope,
evidence request and snapshot, snapshot generation, reservation, execution
history, replay checkpoint, reconciliation generation, coverage, record count,
and outcome. The
reconciliation fingerprint is the signed attestation subject. Only `matched`
with complete coverage is structurally acceptable; missing, duplicate,
conflicting, unresolved, stale, substituted, or incomplete evidence is blocked.

Authentication proves only that the configured key signed the immutable
payload. It does not prove snapshot completeness, broker truth, or independent
reconciliation. The repository contains no operational authenticated broker or
reconciliation authority.

## Provider protocol and availability

The protocol uses client-generated request and challenge identifiers, canonical
request fingerprints, domain-separated capabilities, exact subject/scope and
policy bindings, Ed25519 signatures, validity and freshness intervals, monotonic
source sequences, and atomic challenge/sequence consumption.

`ExternalTransportContext` documents bounded connect, read, overall, and
concurrency limits. Automatic retries are zero. The existing provider boundary
defaults to no I/O, has a two-second read deadline and thirty-second hard limit,
and caps abandoned daemon calls at four. Timeout, capacity exhaustion,
exceptions, malformed responses, and unavailable dependencies return sanitized
reason codes. Late responses are discarded.

The transport context deliberately has `transport_authenticated=false`. A real
integration must independently authenticate the endpoint and bind that identity
to the configured statement authority. Configuration alone is not a handshake.

## Operational runbook

1. Keep production, demo, and live submission disabled.
2. Provision the external verification root through an independently reviewed
   second channel; record no private key in FRIDAY.
3. Validate provider identity, certificate/transport policy, signing key,
   capability scope, lifecycle, and rotation procedures independently.
4. Configure a separately administered monotonic witness and rehearse all
   unknown-outcome recovery cut points.
5. Configure authenticated broker evidence and reconciliation providers with
   read-only credentials outside the repository.
6. Run duplicate, rollback, outage, timeout, revocation, substitution, and
   reconciliation exercises against a non-trading environment.
7. Obtain independent security and production-boundary review.
8. Do not enable execution merely because readiness diagnostics improve.

Recovery never resets replay history, lowers a witness checkpoint, accepts a
new root without approved lineage, or reactivates revoked trust. Ambiguity is a
blocking reconciliation event.

## Remaining blockers

- independently authenticated root provisioning and recovery ceremony;
- operational authenticated lifecycle/revocation provider;
- separately administered monotonic witness and durable external replay state;
- authenticated read-only broker/account evidence source;
- independently authoritative reconciliation source;
- credentials, endpoint configuration, monitoring, incident response, and
  independent production security approval.

Until every blocker is operationally implemented and independently verified,
production authorization, submission, and execution remain unavailable.
