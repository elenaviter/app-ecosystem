# Issuer-managed Card writes

## 2026-10-05: portable gate, unreleased phase-one contract

The portable Card service does not interpret an external issuer's policy.
Credentialless Cards with an external, opaque issuer require a trusted
`IssuerRegistry` adapter before update or revoke. Unknown issuers refuse.
Ordinary owner-managed application and descriptor Controls retain their
existing local checks; explicit adapter registration overrides that exception.
Ownership or a platform administrator role is not an external issuer decision.

The gate supplements, rather than replaces, current Card ownership, catalog,
subset, snapshot and revision checks. A managed legacy snapshot cannot be
silently repaired by an ordinary edit: it returns
`issuer_snapshot_requires_explicit_migration` before mutation. An explicitly
authorized migration remains a separate operation.

## Evidence and wire contract

`IssuerRequest` binds authenticated actor, request id, action, access id,
current Card revision, opaque issuer kind/ref, canonical change digest and
server-owned context reference. The digest is SHA-256 of the actual
server-built candidate `CardAuthority.to_dict()`, serialized with sorted keys,
compact separators, UTF-8, `ensure_ascii=False` and `allow_nan=False`. Revoke
hashes exactly `{action: "revoke", access_id, card_revision}`.

Frozen cross-implementation candidate vector:
`{"label":"编辑 — ü","properties":{"scope":"权限"}}` has digest
`d5564400ec5350c6b10e7bc5b051af29b8115ccf918e6e9ecc6f299ad40d1e1d`.
The authority owner imports `change_digest` or implements these exact bytes;
ASCII-escaped JSON is not this candidate policy digest.

The optional prepare port receives the actual current and candidate authority,
not a client digest standing in for policy evidence. The authority owner
recomputes the digest, resolves its own target identity and fresh policy,
and stores an exact, bounded reservation. It returns an opaque `context_ref`.

`remote_issuer.py` provides `sign_issuer_request` / `verify_issuer_request` and
`sign_issuer_envelope` / `verify_issuer_envelope`, using the existing generic
`ServiceProof` contract. Recipient bundle and operation, expected workload
identity, protocol and complete payload are signature-bound. Every call needs
a fresh nonce. The receiving authority must claim nonces in shared/durable
storage; these helpers do not implement replay storage.

| Port | Protocol | Signed payload (excluding service_proof) |
| --- | --- | --- |
| prepare | `issuer-context.v1` | request with empty context_ref, current, candidate |
| decide/revalidate | `issuer-decision.v1` | request with server context_ref |
| finalize | `issuer-outcome.v1` | request, outcome with state and card_revision |

Every port constructs `AdmissionRequest(resource=bundle_id, operation=operation,
invocation_id=request_id, request_digest=SHA256(canonical payload),
approval_context={protocol})`. The canonical payload is the full request for
decide, or the complete prepare/finalize payload excluding service_proof.
Wire digest JSON uses sorted keys, compact separators, UTF-8,
`ensure_ascii=True` and `allow_nan=False`; `issuer_payload_digest` owns that
serialization. Full evidence remains in the body and verification recomputes
its digest: a caller digest alone is never evidence. Bounded admission context
contains only the small protocol tag, never serialized Card JSON. Existing
invocation/context validation is not widened or skipped. `delegated_token` is
`<protocol>:<request_id>`. This wire digest is intentionally distinct from the
UTF-8 candidate policy digest serialization.

Decide replies echo the exact request and contain only `allowed`, `reason`,
`policy_version`, and aware `valid_until` in their decision. The registry seals
the authenticated result in process; no client JSON is a sealed receipt.
Proofs bind all coordinates plus the registry and adapter instances. A copied
seal attached to changed fields is refused. Validity is at most 60 seconds,
and fresh revalidation cannot extend the initial window or accept a changed
policy version.

## Target commit and outcome

### Caller-recorded revoke target

`AutomationAccessService.revoke_access` accepts `expected_access_id` and
`expected_card_revision`. Both are required for externally issuer-managed
Cards. A supplied revision is a positive JSON/Python integer: booleans,
floats and strings are not silently coerced. Ordinary unmanaged owner writes
retain their older behavior when both fields are omitted; supplying either
field requires the complete valid pair.

The caller records the target before requesting revocation. The expected ID
must equal `access_id`, and the loaded Card must have the expected revision.
The service then hands that same revision to the existing durable revoke
port, which compares it again inside the target mutation lock. It never
reloads a replacement and adopts its revision. A replacement before the
initial read or between that read and lock acquisition therefore refuses
without revoking the replacement, cleaning handles or notifying a change.

The app's authenticated, owner-scoped POST operation
`delegated_access_revoke` forwards these fields unchanged. Example body:

```json
{"data":{"access_id":"card-1","expected_access_id":"card-1","expected_card_revision":3}}
```

Issuer request identifiers and approval context remain server-owned. Actor
binding, ownership and the configured issuer decision still apply. These
fields do not create a cross-owner endpoint or a project-policy bypass.
Missing managed preconditions return
`delegated_access_revoke_precondition_required`; an incomplete or malformed
pair returns `delegated_access_revoke_precondition_invalid` (400); a changed
ID returns `delegated_access_revoke_target_mismatch` (409). A moved, absent
or already revoked expected target returns `delegated_card_revision_conflict`
with `card_revision_moved`, status 409 and `retryable: false`. A caller must
reconcile a conflict, not retry revocation with the replacement's revision.

Legacy domain-specific lifecycle operations are not qualified by this port
change. In particular, a multi-Card lifecycle needs its own exact-target and
ordering proof; this contract does not claim a cross-Card transaction.

The durable persistence port invokes `before_commit` inside the target's
shared mutation lock, after checking its current durable revision and before
projection, credential-handle, durable or notification effects. Missing a
guard-capable persistence port refuses. The final recheck is a fresh,
non-consuming authority read. Remote issuer transport is bounded to at most
five seconds and failures refuse; there is no module-level decision, nonce,
or reservation cache in the new issuer modules.

The supported KDCube adapter uses `observed_file_lock_async` on the shared Card
`.mutation.lock`: an advisory filesystem lock held until context exit/process
closure, with a 30-second acquisition wait and **no lease TTL**. A different
host with an expiring lock must qualify a transport timeout strictly shorter
than its lease and ensure the lease remains held throughout the mutation.
No transaction across the issuer store and Card store is claimed: fresh
policy, bounded decision validity and the target revision CAS are distinct
fences. The existing reconciliation/marker steps remain between the recheck
and durable commit; slow storage qualification must cover that boundary.

Finalize reports the exact request and terminal `committed` or `refused`
revision, never grants authority. The authority owner provides idempotent
terminal receipts and prevents reuse of a terminal reservation. A failed
finalize cannot undo a committed Card; `issuer_outcome_confirmed: false`
reports that its remote reservation remains pending until bounded expiry.

## Scope and remaining integration

This phase adds an opaque authorization seam only. It is not completion of
the larger domain migration. The app composes request-local adapters from
trusted `connections.delegated_credentials.issuer_authorities` rows, using
opaque bundle/operation strings and existing workload secret references.
The authenticated session actor is bound independently of any legacy owner
proxy. Malformed declared configuration and unavailable proof secrets refuse.
Live configuration/provisioning, authority-owned context/replay tests and
exact-source mounted PostgreSQL/Redis qualification remain activation gates.
No new browser parameters accept decisions, context references or workload
secrets. This worker has made no credential, grant, restart or runtime
configuration change. The source contract does not alter the operator's
standing scoped deployment authority or the designated executor's duties.
