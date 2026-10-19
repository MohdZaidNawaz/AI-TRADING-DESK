# Phase 13J Windows DPAPI key protection

This phase adds local private-key protection for the Phase 13I personal policy
signing foundation. It does not establish production authority or enable any
execution path.

## Implemented boundary

`WindowsDpapiKeyProvider` uses PyCA `cryptography` for Ed25519 and native
Windows DPAPI for storage protection. The production provider constructs the
native DPAPI, Windows ACL, and PyCA Ed25519 implementations itself; callers
cannot inject a model-shaped protector or permissive ACL and have it reported
as DPAPI protection. DPAPI calls use the current Windows user,
`CRYPTPROTECT_UI_FORBIDDEN`, and no machine-protection flag. Versioned entropy
binds the full persisted public descriptor and lifecycle record, including
schema versions, algorithm, key ID, generation, public-key fingerprint,
protection type, creation and expiry timestamps, lifecycle state and timestamp,
reason, and replacement identity. Altering any bound field prevents decryption.

The native wrapper validates input and returned buffer sizes, copies returned
data before release, zeroes decrypted native buffers before `LocalFree`, and
zeroes mutable Python private-key and entropy buffers after use. The
cryptography library necessarily creates internal memory objects that Python
cannot guarantee will be wiped immediately; this implementation makes no
stronger memory-erasure claim.

Only JSON containing public metadata and a DPAPI ciphertext is written. The
default location is:

`%LOCALAPPDATA%\NimbusDesk\FRIDAY\PersonalPolicyKeys`

The provider applies and verifies an exact protected Windows DACL granting full
access only to the current user, SYSTEM, and Administrators. Extra or missing
ACEs, altered rights, deny entries, unexpected inheritance, and unprotected
DACLs fail verification. It refuses malformed or oversized records, invalid
ACLs, DPAPI failures, entropy mismatches, public-key mismatches, duplicate key
creation, and invalid lifecycle transitions. There is no plaintext fallback
and no implicit key generation after loss or decryption failure.

## Lifecycle behavior

- Creation is exclusive and never overwrites an existing key record.
- Public export returns only the immutable public descriptor.
- Signing decrypts the key for one operation and re-derives the public key
  before use.
- Rotation is explicit. The predecessor is retired and its ciphertext removed
  before the replacement is persisted. An interruption can therefore require
  recovery, but cannot leave the old and replacement generations silently
  usable together.
- Revocation is explicit and removes the protected private blob.
- Recovery is diagnostic only. It never regenerates or substitutes a key.

Lifecycle operations cooperate through a protected one-byte Windows file lock,
so provider instances and processes using this implementation serialize local
creation, signing, rotation, revocation, export, and recovery checks. The lock
does not turn the files into a transactional multi-record authority database:
rotation still intentionally retires the predecessor before creating the
replacement, interruption can require explicit recovery, and a user with file
access can roll local records back. These remain personal custody records, not
an authoritative trust or revocation registry.

## Security meaning and remaining blockers

Windows DPAPI protects an exportable secret at rest using the current user's
Windows security context. It is not hardware-backed storage, a non-exportable
CNG key, an HSM, or independent governance. Local possession of the protected
key proves neither independent authorization nor broker truth.

Production readiness remains blocked until a genuine personal trust bootstrap,
authoritative and fresh revocation/replay state, approved compromise recovery,
trusted broker/account evidence, authoritative reconciliation, submission-
boundary review, and the required production approval exist. The Phase 13G
readiness gate remains unchanged. No Phase 13J component is composed into
Sentinel approval, Pilot, `FinalExecutionGate`, reservation/execution state,
MT5, broker adapters, public APIs, or the frontend.
