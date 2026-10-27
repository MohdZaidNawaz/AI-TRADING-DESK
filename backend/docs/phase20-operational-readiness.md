# Phase 20 operational readiness and deployment planning

Phase 20 adds local, provider-neutral readiness diagnostics and operational
planning. It does not deploy FRIDAY, provision an authority, connect a broker,
or authorize an order. Every environment remains non-executing.

## Capability labels

- **Implemented locally:** immutable readiness evidence/check/report models,
  deterministic evaluation, bounded reason codes, secret-safe references, and
  sanitized operational signal names.
- **Tested synthetically:** failure classification, stale/conflicting evidence,
  fake-source rejection, immutable false authority flags, and log redaction.
- **Designed, not operational:** staging/production topology, monitoring,
  backup, recovery, incident response, and external acceptance procedures.
- **External:** trust root, lifecycle/revocation service, rollback witness,
  broker evidence, reconciliation, alert delivery, and independent review.
- **Manual:** security acceptance, change approval, deployment approval, and
  explicit production authorization. None has occurred.

## Diagnostic architecture

`OfflineOperationalAcceptanceEvaluator` emits one deterministic check for every
prerequisite. It is an offline acceptance assessment, separate from both the
existing runtime `OperationalReadinessChecker` and the `/api/readiness`
production-authorization diagnostic. It is not an API or execution gate.
Missing evidence is
`NOT_ASSESSED`; explicit absence is `UNAVAILABLE`; malformed or contradictory
evidence is `INVALID`; expired evidence is `STALE`; policy or authority gaps are
`BLOCKED`. Caller-created local, synthetic, external, and manual evidence claims
never become verified observations or satisfy a prerequisite.

The only concrete trusted local verifier currently integrated is the exact
`DurableIndependentReplayStore` read-only diagnostics interface. It may satisfy
local restart durability and atomic-consumption health when its schema and store
lineage validate. This does not establish external rollback resistance. Audit
and redaction activation, runtime clock health, provider enforcement,
monitoring, backup/recovery rehearsal, and execution-control state have no
complete trusted verifier in this assessment and remain unavailable or not
assessed.

The evaluator invokes the concrete diagnostics implementation through the
class-level method, so an instance-shadowed `diagnostics` attribute is ignored.
It requires the exact immutable diagnostics model, the exact evaluation
timestamp, validated store identity/status fields, and a canonical 64-character
fingerprint. Malformed, stale, future-dated, unexpected, or exceptional results
fail closed without exposing exception text. These checks trust the loaded
application code, the process, SQLite implementation, filesystem, and current
store path. They do not authenticate the host, protect against privileged local
mutation, or detect coherent local snapshot rollback. Only an independently
administered external witness can provide that separate assurance.

The aggregate status is always `blocked`. `production_authorization_available`,
`submission_allowed`, and `execution_authority` are literal false. This model is
not wired to Pilot, FinalExecutionGate, MT5, an API submission route, or any
provider client.

## Environments and deployment boundary

| Environment | Permitted capability | Prohibited capability | Current state |
| --- | --- | --- | --- |
| Local development | deterministic models, unit tests, mock market UI | external authority, broker access, submission | implemented |
| Synthetic integration | isolated fake signatures/providers and disposable databases | production credentials or claims | implemented in tests |
| Isolated staging | future authenticated services, non-trading accounts, monitoring rehearsal | live orders and production authority | not provisioned |
| Production | future independently reviewed topology | any activation without every gate and separate authorization | unavailable |

A future deployment must use secret injection outside source control, pinned
service identities, least-privilege read-only broker credentials, explicit
network allowlists, bounded provider timeouts/concurrency, durable replay
storage, an independently administered witness, sanitized logs, alert routing,
backups, and dual-controlled change approval. Static configuration cannot
activate trading.

## Configuration and secret acceptance

Configuration must be schema-versioned and drift-detectable. Required settings
include provider and trust identities/scopes, allowed Ed25519 keys and
fingerprints, bounded connect/read/overall timeouts, concurrency, freshness and
clock-skew policy, replay-store identity/path, witness checkpoint policy, audit
sink, and redaction policy. Missing, blank, conflicting, out-of-range, unknown-
version, or unsupported-algorithm settings fail closed.

Secrets are injected at runtime and never emitted in errors, reports, tests, or
logs. `.env` presence proves neither validity nor authentication. Rotation and
emergency revocation require independent approval, exact lineage, rollback
protection, rehearsal, and a documented recovery path. There is no automatic
replacement key, default credential, plaintext fallback, or cached-success
fallback.

`OperationalRuntimeConfiguration` implements the non-secret policy portion: a
fixed disabled execution mode, a revision fingerprint, connect/read/overall
timeouts, concurrency, clock skew, and freshness bounds. Overall timeout must
cover each component timeout. A valid instance proves only that this supplied
policy object is structurally valid. It does not prove the running process
loaded it or that clocks, providers, audit, recovery, or execution controls
enforce it. The configuration check therefore remains blocked as
`CONFIGURATION_POLICY_VALID_RUNTIME_UNVERIFIED`.

## Monitoring and alerting contract

`OperationalSignal`, `OperationalEventState`, and `OperationalReasonCode`
supply finite vocabularies for provider availability, trust
root and lifecycle state, replay integrity/rejection, witness disagreement,
broker evidence, reconciliation, readiness changes, capacity, configuration,
and recovery. Events contain stable reason codes and sanitized bounded fields,
never credentials, raw signed statements, private keys, tokens, or raw broker
payloads. Unknown labels are rejected. Extending a vocabulary requires source,
test, documentation, and alert-routing review; arbitrary runtime labels are not
accepted.

The event factory requires the exact enum type for all three labels before
reading their values. Strings, foreign enums, and signal-like objects are
rejected. New labels require a reviewed enum, tests, documentation, and alert
routing update rather than runtime-generated values.

| Severity | Examples | Required action |
| --- | --- | --- |
| INFO | expected blocked startup, completed local check | retain sanitized audit evidence |
| WARNING | provider timeout, stale evidence, capacity exhaustion | keep blocked; investigate within support window |
| ERROR | replay corruption, witness conflict, compromise, reconciliation mismatch | contain immediately; require independent review |

Monitoring or alert delivery failure is itself blocking. No monitoring backend
or alert destination is connected in this phase.

## Backup and recovery

Backups must be encrypted, access-controlled, versioned, periodically restored
in isolation, and bound to the exact schema and replay checkpoint. A restored
database is not independently rollback-safe until the external witness accepts
its checkpoint. Never reset sequence heads, delete replay history, lower a
witness checkpoint, or silently recreate authority state. Commit uncertainty,
conflicting checkpoints, and incomplete restoration enter reconciliation.

Recovery evidence includes the pinned commit, configuration fingerprint,
database integrity results, schema version, replay/history checkpoint, witness
response, audit/outbox state, and independent approvals. Production remains
disabled throughout rehearsal and recovery.

## Incident response runbooks

Every incident preserves sanitized evidence, disables authorization/submission,
forbids destructive reset, requires an independent approver, and returns to
normal only after the affected authority is current and independently verified.

| Incident | Detection | Immediate containment | Recovery and continued-block criteria |
| --- | --- | --- | --- |
| Trust-root compromise | fingerprint/lifecycle/alert mismatch | revoke root; isolate providers | new independent ceremony and lineage; block while ambiguous |
| Signing-key compromise | invalid or unexpected signatures/sequence | revoke key and stop provider acceptance | independently approved rotation; never self-approve |
| Revocation-provider outage | unavailable/stale lifecycle | reject cached success | current authenticated response required |
| Witness outage | unavailable update | stop progression | same idempotency key and verified checkpoint required |
| Lower/conflicting witness | checkpoint disagreement | declare rollback/equivocation incident | independent reconciliation; never lower local head |
| Replay corruption | integrity/schema/history failure | isolate database and preserve copy | verified restore plus external witness acceptance |
| Uncertain commit | missing acknowledgement | reconciliation-required; no retry with new identity | prove durable outcome before progression |
| Broker mismatch | source/account/snapshot mismatch | reject evidence | new authenticated complete snapshot and reconciliation |
| Unresolved reconciliation | missing/duplicate/conflicting records | block reservation/submission | authoritative matched complete coverage |
| Timeout/capacity exhaustion | stable provider reason code | shed load; no unbounded retries | provider health and capacity review |
| Log exposure | redaction alert or secret discovery | stop affected sink; revoke exposed material | scrub under approved retention process and review |
| Execution-path modification | diff/control alert | kill switch; isolate deployment | independent source and boundary review |
| Failed deploy/rollback | health/config/schema failure | keep old safe instance or stop | pinned build and state compatibility verified |
| Disaster recovery | site/storage loss | activate non-trading recovery plan | restored state, witness, providers, monitoring, and approvals verified |

Evidence must not be deleted merely to make a diagnostic green. Any missing
provenance, uncertain persistence, stale authority, or incomplete recovery
requires continued blocking.

## External integration acceptance matrix

| Gate | Required evidence and automated tests | Responsible/manual verification | Current state/blocker |
| --- | --- | --- | --- |
| Independent trust root | identity, key, algorithm, scope, validity, provenance; substitution/rotation/revocation tests | security owner plus second-channel approver | blocked: no independent ceremony |
| Lifecycle/revocation | authenticated signed freshness/sequence; outage/stale/supersession tests; durable challenge consumption | authority operator and security reviewer | unavailable: no operational provider |
| Rollback witness | independent identity and monotonic checkpoint; lower/conflict/crash tests | separate witness administrator | unavailable: local fake/SQLite is insufficient |
| Broker evidence | authenticated read-only account/scope/snapshot; freshness/completeness/replay tests | broker-access owner and reviewer | unavailable: no validated source |
| Reconciliation | exact account, reservation, history, evidence, checkpoint; missing/duplicate/conflict tests | independent reconciliation owner | unavailable: no source of truth |
| Operational controls | alerts, custody, access/audit, backup rehearsal, incident review | operations, security, and production approvers | not assessed and not approved |

Passing synthetic tests is evidence about local code only. A provider cannot
supply or replace its own trust anchor, and a local fake cannot pass an external
acceptance gate.

## Release and deployment checklist

- [ ] Pin and independently review the source commit and complete diff.
- [ ] Run focused/security/full tests and record failures, skips, and limits.
- [ ] Perform dependency and vulnerability review using approved current data.
- [ ] Validate versioned configuration and drift without printing secrets.
- [ ] Verify independent trust-root ceremony and current lifecycle evidence.
- [ ] Verify durable replay integrity and independent rollback witness.
- [ ] Verify authenticated complete broker evidence and reconciliation.
- [ ] Verify sanitized logging, metrics, alert delivery, and health behavior.
- [ ] Rehearse backup restore, uncertain commits, and incident runbooks.
- [ ] Review access control, key custody, rotation, and audit trails.
- [ ] Obtain independent security and change-management approval.
- [ ] Obtain explicit human production authorization in a future milestone.
- [ ] Confirm submission remains disabled unless separately reviewed and changed.

No checkbox, diagnostic, configuration flag, test result, or operator input can
itself enable execution.

## Residual risk and decision

External identity, independent root provenance, current revocation truth,
coherent local-database rollback detection, broker snapshot completeness,
authoritative reconciliation, alert delivery, backup rehearsal, organizational
access control, vulnerability intelligence, independent security review, and
human approval remain unverified or unavailable.

**Production-readiness decision: BLOCKED.** Production authorization,
submission, demo execution, and live execution remain disabled.
