# Sentinel Portfolio-Risk Policy Governance Decision — UNSIGNED TEMPLATE

Status: **UNAPPROVED / NON-AUTHORITATIVE**

This template records the human governance decision required before FRIDAY may
verify a Sentinel portfolio-risk policy as independently authorized. Merely
completing or committing this document does not authorize a policy. It becomes
effective only after the organization identifies a legitimate decision-maker,
establishes the trust anchor independently, issues a verifiable approval
artifact, and configures an authoritative approval/revocation source through a
separately reviewed production integration.

Do not place private keys, passwords, credentials, tokens, recovery material,
or secret-manager values in this document or repository.

## Decision identity

- Governance decision reference: `<REQUIRED>`
- Governance-contract version: `1`
- Decision status: `<UNAPPROVED | APPROVED | REVOKED | SUPERSEDED>`
- Effective date/time (UTC): `<REQUIRED>`
- Expiry or review date/time (UTC): `<REQUIRED>`
- Superseded decision reference, if any: `<OPTIONAL>`

## Authorized approver

- Approver identity: `<REQUIRED — no invented default>`
- Organizational role: `<REQUIRED>`
- Organizational authority establishing this role: `<REQUIRED>`
- Permitted capabilities: `<APPROVE_POLICY, REVOKE_POLICY, SUPERSEDE_POLICY>`
- Independent reviewer identity/role: `<REQUIRED>`

## Approval scope

- Permitted policy IDs: `<REQUIRED>`
- Permitted policy versions/version rules: `<REQUIRED>`
- Permitted environments: `<REQUIRED>`
- Permitted account identities: `<REQUIRED>`
- Permitted canonical instruments: `<REQUIRED>`
- Explicit exclusions: `<REQUIRED>`

## Trust anchor

- Mechanism: `<ED25519_PUBLIC_KEY | EXTERNAL_GOVERNANCE_SERVICE>`
- Public key or external-service identity: `<REQUIRED>`
- Verification key/service identifier: `<REQUIRED>`
- Public identity fingerprint: `<REQUIRED>`
- How identity was established independently: `<REQUIRED>`
- Key custody or external-service owner: `<REQUIRED>`
- Rotation and compromise procedure: `<REQUIRED>`
- Trust-anchor expiry/review date: `<REQUIRED>`

Private signing material must remain outside the repository. A new key is not
trusted merely because software generated it or configuration references it.

## Approval artifact and revocation process

- Approval artifact schema/version: `sentinel-policy-approval/v1`
- Canonical policy fingerprint procedure: `<REQUIRED>`
- Artifact issuance procedure: `<REQUIRED>`
- Durable authoritative artifact source: `<REQUIRED>`
- Durable current revocation/supersession source: `<REQUIRED>`
- Revocation authority and emergency procedure: `<REQUIRED>`
- Conflicting-artifact resolution procedure: `<REQUIRED>`
- Historical retention period: `<REQUIRED>`

## Audit and change control

- Audit owner: `<REQUIRED>`
- Independent review cadence: `<REQUIRED>`
- Artifact and lifecycle retention requirements: `<REQUIRED>`
- Incident and reconciliation owner: `<REQUIRED>`
- Change-control and reapproval procedure: `<REQUIRED>`
- Evidence that the decision was approved outside this repository: `<REQUIRED>`

## Required technical follow-up after legitimate approval

1. Configure the independently established public identity or external service
   through a reviewed production trust mechanism; do not store private material
   in Git or expose it through logs/readiness output.
2. Implement the production governance and approval-lifecycle sources against
   the authoritative durable systems named above.
3. Verify exact approver, scope, policy ID/version/fingerprint, validity,
   revocation, supersession, and trust-anchor bindings.
4. Re-audit startup composition so no fixture, local declaration, or caller-
   supplied protocol can be selected as production authority.
5. Complete the unresolved atomic lifecycle design before enabling any
   submission path.

Until every step is independently reviewed and validated, production policy
resolution, controlled-demo execution, and live execution remain blocked.
