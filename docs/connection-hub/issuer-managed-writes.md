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

## 2026-10-06: draft two-Card lifecycle (not independently qualified)

This is a separate generic transaction, not a loop over the one-Card revoke
port above. The hosted alias is `issuer_managed_lifecycle_apply`, operations
route, POST. Invoke it through the existing request-bound SDK
`call_bundle_operation`; no private Hub instance is handed to another app.

The DTO has exactly `context_ref`, `request_id`, `action: "revoke"`,
`change_digest`, and exactly two `targets`. Each target has exactly
`owner_subject`, `access_id`, `expected_card_revision` (positive exact integer),
`expected_authority_fingerprint` (full `CardAuthority.content_hash()`),
`issuer_kind`, and `issuer_ref`. Fingerprints include the full properties,
provenance and opaque Card bindings. No substitute/reloaded target is adopted.
The UTF-8 candidate digest hashes `{action: "revoke", targets: <complete target
objects sorted by (owner_subject, access_id)>}` using `change_digest`.

Coordinates authorize nothing. The hosting operation positively requires a
registered/privileged platform-human protected context. External/delegated
principals refuse even when their grantor user id equals a target owner.
SDK-injected `user_id` and `fingerprint` method metadata are explicitly ignored;
they never select the actor. Extras retained in a nested DTO, including actor,
owner proxies and approval hashes, are rejected before service construction.
Both configured issuers receive the actual actor, whole pair digest, original
request/context and their own exact target. Sealed decisions are revalidated
under both Card fences before any effects and again before publication;
revalidation never extends the initial, at-most-60-second window.

Service callable:
`DelegatedCardService.revoke_lifecycle(request, *, actor_subject, before_commit,
after_commit)`. `before_commit` receives the two current authorities and is the
configured issuer closure. `after_commit` receives their before authorities
and performs idempotent credential-handle cleanup. `DurableCardPersistence`
supplies that cleanup. This low-level service is not an authentication API.
`before_commit` must return the earliest aware `valid_until` of its freshly
sealed decisions, not `None`. The service clamps its final deadline to that
original value; the commit IO thread checks it AFTER writing the temporary
file and immediately BEFORE the visibility rename. A stalled temporary-file
write cannot silently extend issuer approval.

The service acquires a durable receipt fence, then the existing production
`.mutation.lock` fences in `(subject_hash, access_id)` order. One 30-second
forward-progress deadline covers acquisition, checks, staging and cleanup; flock itself
has no TTL or steal. An explicit trusted host capability is required:
`connections.delegated_credentials.lifecycle_storage.lock_scope` is either
`same-host-flock` or `shared-flock-verified`. Neither is inferred from a path.
No capability, unsupported object storage, or unverified cross-host NFS/SMB
behavior refuses. This draft has not changed any live configuration.

The 30 seconds is a forward-progress deadline, not forced cancellation of an
already-started kernel or database operation. Lifecycle-scoped writes, Redis
mutations and handle cleanup are shielded and drained BEFORE either Card
fence is released. A stuck backend may extend wall-clock drain time; a host
profile must qualify that bound, and an unverified/hung backend is not claimed
supported. A timeout after durable commit returns committed/serving-pending,
never no-write refusal. An interrupted preparation remains recoverable.

The absolute filesystem backend publishes with a same-directory atomic
`Path.replace`, through `write_json_atomic`. Power-loss/fsync durability is
not claimed. A bounded active-intent directory publishes the complete pair
before either version or pointer is staged. Both participants refuse a
single-Card writer with retryable `lifecycle_preparation_unresolved` while
preparation or serving completion remains unresolved. An overloaded recovery
queue refuses rather than scanning an unbounded receipt history.

Pending current pointers resolve the before revision until one shared receipt
rename records `committed`; thereafter both resolve the after revisions.
Current pointers are re-read and compared with the recorded before value
before staging. A per-version lifecycle sidecar, installed before its version,
keeps uncommitted/aborted versions out of explicit history, listing and initial
migration reads. Missing or corrupt bindings fail closed.

Redis installs both permanent updating markers in one Lua operation and
invalidates both the serving epoch and the sweep token. Rebuilds cannot bless
an unresolved lifecycle. This temporarily closes the entire serving partition
until its normal durable sweep can prove readiness; it is not merely a
two-key cache optimization. Redis Cluster keys in different slots refuse
`CROSSSLOT`; there is no sequential fallback. The service positively checks
Redis mode before publishing any active intent or changing a cache/Card. Cluster
or unverified mode records a terminal preflight refusal under the receipt/Card
fences, with no staged version/pointer or marker. Identical retry returns that
refusal without cleanup. Cluster profiles are unsupported; no key migration is
performed. A transient mode-discovery failure is also a terminal preflight
refusal for that request: restoring Redis does not make an identical replay
resume. Reconcile the refusal, then use a new request id for a new mutation.
For older refused cluster intents, release may complete ONLY after
confirmed cluster mode and individual reads prove this transaction owns no
marker. Owned, malformed, unavailable or unproven markers remain fail-closed;
a generic EVAL failure is never ignored. This legacy release proves only the
absence of this transaction's two Card markers; it does not certify the whole
serving partition or make Cluster a supported mutation profile.
No real-Cluster qualification of this
new guard is claimed by portable unit tests.

Receipt state and serving completion are separate. `committed/pending` means
both authority revisions were revoked but cleanup is incomplete: HTTP-style
status 202, `ok: false`, `retryable: true`. It is NEVER a no-write refusal or a
reason to compensate authority. Both participants remain closed. Recovery of
the identical request holds the same three production fences, retries cleanup
and atomically finalizes both tombstones before recording `serving_state:
"complete"`. Changed replay bodies at the same actor/context/request key
refuse. Completed replay returns its historical receipt without deleting
handles or mutating a later legitimate Card revision.

An interrupted preparation is deterministically aborted under both fences.
Identical retry then returns the recorded refusal; a new mutation attempt
requires a new `request_id`, not an automatic retry of that reservation.
Issuer-owned orchestration consumes the consolidated two-target result; Hub
does not manufacture a second per-target domain finalization protocol.

The source is still a draft. Disposable child-kill storage tests, direct
handler identity regressions and service unit tests are not production-fence,
real Redis/PostgreSQL or actual mounted-authentication proof. Exact-source
independent review and those gates must precede composition/activation.

## Protected two-Card identity read (draft, separate from writes)

`issuer_managed_lifecycle_read` is a POST operations alias, registered/privileged
only, with CSRF protection. Its strict DTO has only `context_ref`, `request_id`
and exactly two targets, each containing only `owner_subject`, `access_id`,
`issuer_kind`, `issuer_ref`. Targets sort by `(owner_subject, access_id)` and
duplicate IDs refuse. Context is an opaque canonical JSON object string, at
most 4096 UTF-8 bytes. It is a selector, NEVER a reservation or authorization.

The host positively binds actual platform-human identity/classification and
actual runtime tenant/project; absent scope refuses without a default. SDK
`user_id`/`fingerprint` metadata is ignored. Delegated/external identities,
including owner-equal grantors, cannot borrow a human classification. The
request-local read registry has NO owner-managed exception.

The separate `issuer_read` module exports `IssuerReadRequest`,
`IssuerReadDecision`, `issuer_read_request_from_mapping`,
`sign_issuer_read_request`, `verify_issuer_read_request`. The request wire has
`actor_subject`, `actor_classification` (`registered` or `privileged`), `tenant`,
`project`, `context_ref`, `request_id`, `targets`, and `read_digest`.
`read_digest` hashes `{protocol: "issuer-read.v1", request: <all other fields>}`
with sorted compact JSON, literal UTF-8 (`ensure_ascii=False`), and no NaN.
Read types/seals/protocol are not compatible with write requests or decisions.

Signed peer bodies have `request`, `phase` (`authorize` or `validate`),
`snapshots`, and `service_proof`. Authorize evidence is `[]`; validate evidence
is exactly two snapshots in request target order. Each snapshot has `target`
(the exact coordinate object), `card_revision`, `authority_fingerprint`, and
`identity`. Recipient, operation, expected service identity, protocol and ALL
request/evidence bytes are signature-bound. Every peer call uses a fresh nonce.
**The verifier only authenticates. The issuer endpoint MUST atomically consume
the nonce in shared durable storage before evaluating policy.** Cross-process
replay qualification belongs to that endpoint, not an in-process helper cache.

Reply shape is `ok: true`, exact `request`, exact `phase`, `snapshots_digest`
(the same literal UTF-8 digest of snapshots), and `decision` with only
`allowed`, `reason`, `policy_version`, aware `valid_until`. Fresh read decisions
are registry/adapter-local seals, valid for at most 60 seconds. The second
decision must retain the policy version and cannot extend the first expiry.
All decisions are checked again after the last peer await.

Ordering is first sealed peer authorization, then BOTH production Card fences
in `(subject_hash, access_id)` order, then raw committed snapshots, RELEASE BOTH
fences, and finally sealed peer revalidation bound to both snapshots/full hashes.
No peer I/O occurs inside either Card fence. One 30-second forward-progress
deadline covers the whole read. `read_lifecycle_identities(request)` on
`DelegatedCardService` and `DurableCardPersistence` bypasses resolvers/cache
restoration and performs no repair, receipt, revision, handle or Redis write.
Prepared/unresolved state returns retryable `issuer_read_lifecycle_pending`;
recovery remains separately authorized. Missing explicit shared-flock capability
or a non-atomic filesystem backend refuses. A failure returns no snapshots.

The safe base projection is `access_id`, `grantor_subject`, `delegate_subject`,
`source`, `card_kind`, `card_revision`, `state`, `issuer_kind`, `issuer_ref`,
`composition_mode`, `identity_scope`; `control_card` exports only `control_id`,
`issuer_kind`, `issuer_ref`. Trusted issuer descriptors may approve additional
scalar identity leaves via `read_identity_leaf_paths` (arrays of path segments
under `properties` or `provenance`); never parent maps or caller-selected paths.
Unknown keys, client identifiers/metadata, labels, credentials, tokens and
handles are not copied. `read_operation` is a separately configured capability;
the write operation does not imply it. No live descriptor change is made here.

The full fingerprint is the ORIGINAL `CardAuthority.content_hash()`, not a hash
of the redacted view. An omitted-field change can leave the projection unchanged
while changing that fingerprint. Issuer orchestration records this exact pair;
the later write re-reads full authorities and refuses stale intent, including
replacement/rejoin, before effects. No reservation, membership change, Card
write, grant, finalization or bootstrap occurs during read. This is an internal
authenticated orchestration result, not a browser/Team listing API.

The source and focused author tests do not qualify actual mounted human
authentication, PB fresh policy/durable cross-process nonce storage, or the
combined candidate. Those remain exact-source independent activation gates.

## Separately authorized full snapshots

`issuer_managed_card_snapshots` is a separate POST operations alias with CSRF
protection and registered/privileged platform-human context. It accepts the
strict coordinate query above, but uses `IssuerSnapshotRegistry`,
`issuer-snapshot.v1`, and the separately configured `full_snapshot_operation`.
Neither an identity-read capability nor a write capability implies full export.
See [Issuer full Card snapshot](issuer-full-snapshot.md) for its one authoritative
payload, credential-refusal, ordering and proof contract.

Trusted server orchestration must wrap its existing request-bound SDK call in
`bind_issuer_snapshot_orchestration()` from the public `issuer_snapshot_host`
module. No HTTP header, cookie, bearer or query field can bind that scope; a
direct browser/HTTP call refuses before service construction. This is delivery
confinement, not issuer authorization. The calling app must retain the raw
personal payload internally and expose only its separately authorized result,
never the complete snapshots. Nested calls restore the outer scope; exiting
or cancelling the caller revokes an inherited child's scope as well. The alias
rechecks scope and actual human/runtime context after awaits and before return.

The app builds a fresh registry from the same request-frozen descriptor rows,
resolves existing workload secrets only for each signed peer call, and binds
actual session identity/classification and runtime tenant/project. If that
context changes during service construction, the alias returns
`issuer_snapshot_context_changed` before export. SDK method metadata is ignored;
extra fields inside the query are rejected before service construction.

The service delegates to the separate package contract and the existing fenced
storage read; it does not implement a second snapshot policy or a mutation.
The result is for authenticated internal issuer orchestration. Do not expose
it as a widget or Team inventory response: it contains personal data and the
complete original authority, or refuses both Cards without redaction.

The package contract has independent source approval. New host wiring and its
author tests are not mounted authentication, issuer cross-process replay,
combined-candidate qualification or activation. No live configuration changes
are part of this source composition.

## Exact widening issuer update

`issuer_managed_card_update` is a separate POST operations alias for internal
server orchestration. It requires the actual registered/privileged human,
actual runtime scope, CSRF declaration and the public
`bind_issuer_update_orchestration()` scope from `issuer_update_host`. No request
field or HTTP header can bind that scope. Its full Card result must remain
inside the calling server; it is not a widget response.

The strict query is `{context_ref, request_id, target, delta}`. `target` contains
exactly `owner_subject`, `access_id`, `issuer_kind`, `issuer_ref`,
`expected_card_revision` and `expected_authority_fingerprint` (the complete
original authority hash). `delta` contains exactly `resource`, `operations`
and `grants`: sorted unique string lists describing the desired final
selection of that one resource. Extra actor, approval, decision, candidate,
digest or credential fields refuse before service construction. The operation
never creates a Card or updates a credential handle.

This first version only widens both selections. It preserves every other
resource, identity, issuer/provenance field, property, catalog acceptance,
account and named-service selection, lifetime and credential metadata. Only
the selected resource, its derived flat operation union and revision change.
Narrowing and an empty delta refuse. Legacy payloads whose original durable
fingerprint differs from their normalized model require a separate migration;
this API does not silently rewrite them. Credential-shaped material in the
original authority also refuses before mutation, never redacts the result.

The actual complete candidate is built under the production Card fence. Its
digest is bound into `IssuerRequest(action="update")` for the configured write
issuer. The opaque `context_ref` must identify that issuer's immutable intent;
the issuer owns its approval, current policy, caller authority and reservation
rules. A supplied context authorizes nothing by itself. Hub takes a fresh
sealed decision, then revalidates inside the same Card fence immediately before
publication. The second decision cannot extend the first decision's expiry,
and the publication deadline cannot outlive the original Card's lifetime.
The actual file publication thread checks that deadline after writing its
temporary file and directly before the visibility rename.

Issuer calls occur while the receipt and Card fences are held. Each remote
issuer call is bounded to five seconds; the 30-second forward-progress
deadline covers acquisition and orchestration, not forced cancellation of a
started storage operation. A slow or hung backend must drain before unlock
and remains an explicit host qualification limit.

The durable replay key is `(actual actor, context_ref, request_id)`. Its receipt
binds the **entire** query. Reusing the key with a changed target, fingerprint
or delta refuses; an identical retry recovers the original outcome and never
applies a second revision. Each individual Card update needs its own stable
request ID. This differs from the read-only snapshot request ID, which does
not consume a mutation receipt. Retrying a terminal refusal requires a new
request ID, not a restored provider under the old one.

The service requires the named filesystem atomic-rename backend and a verified
same-host/shared flock capability. It holds receipt and production Card fences
through checks, staging and serving completion. A durable intent precedes every
revision/pointer write and blocks competing ordinary writers and revocations
until recovery completes. The prepared pointer resolves the original Card;
one committed receipt rename exposes the new revision. Staged revisions are
absent from history and explicit revision reads before that commit. Interrupted
preparation recovers as a terminal refusal; a committed receipt is never undone
or relabelled after a later failure. Started file/Redis writes drain under both
fences on cancellation or timeout; a hung backend can exceed the logical
deadline, and the service must not steal its lock.

An expired cache marker can have been read-through restored to the exact
original Card before a no-write refusal finishes. Recovery recognizes that
exact original revision and complete fingerprint as a finished removal,
retires the refused intent and permits later writers. A foreign, malformed or
unavailable cache entry remains unresolved; recovery never deletes it blindly.

Identical terminal replay also retries removal of its exact active-intent file.
A kill or unlink failure between the completed receipt rename and retirement
must not permanently populate the bounded active queue. Retrying retirement
does not rewrite serving state or the original outcome, reauthorize a mutation,
or overwrite a later legitimate Card revision.

A committed update whose cache/index completion or issuer finalization remains
pending returns `202`, its committed state and a retryable outcome—not a
no-write failure. Identical retry finishes that work without fresh mutation
authority. A later legitimate revision is never overwritten by replay. The
complete original after-result is read from the receipt's immutable revision,
not whichever revision is current at retry time. Context movement after commit
retains commit truth but withholds the full Card result. No handles are removed,
rewritten or minted by this path.

This is source/API work, not mounted approval, issuer nonce-store/replay,
live-data/bootstrap or activation qualification. It changes the durable pointer
reader contract, so a reviewed combined reader/writer rollout is mandatory;
old readers fail closed on the new pointer schema. No live config, credentials,
data or runtime changes are authorized by the source change.

### Disposable real-backend UPDATE recovery witness

The app test `tests/test_issuer_update_real_backend_recovery.py` runs the
production SDK persistence and ordered file fences, filesystem Card/receipt
storage, Redis Lua cache transitions and Redis grantor index. Its caller pins
the App Ecosystem and SDK source overlays. Each boundary starts a new child,
kills only that child, then uses fresh processes for reads, competing writes,
identical recovery and changed replay. It covers intent, marker, sidecar,
revision, prepared pointer, committed receipt, projection and completed receipt;
the final case creates a legitimate later revision before recovery. Additional
cases cover real marker expiry/read-through, a foreign marker that must remain
untouched, and loss of a committed projection. The same cases are available for
standalone Redis and Redis Cluster; a missing backend is reported as a skip,
not real-backend evidence.

No backend is provisioned or restarted by the tests. The fixture owner must
first identify a dedicated disposable test server, never a deployed server,
and provide an existing non-production loopback target. Then set `REDIS_URL`
for standalone Redis or `REDIS_CLUSTER_NODE` for Cluster, together with
`CONNECTION_HUB_TEST_DISPOSABLE_REDIS=1`. Targets carrying credentials, remote
hosts, malformed paths or the conventional deployed Redis port 6379 refuse
before connection. Explicit confirmation is a safety gate, not proof that the
server is disposable; that proof belongs to the fixture owner's record.

Use the project's prepared interpreter and exact source overlay, with pytest's
base temporary directory inside the caller's registered scratch run. Each case
uses a fresh random synthetic tenant and a fixture-owned storage root. Cleanup
deletes only the exact Card/index/epoch/lock keys of that namespace, one key at a
time for Cluster; it never scans or flushes a database. The previous source can
be run through the same test files with its package overlay to establish the
serving-complete boundary's baseline failure without editing production code.

The external issuer is still a synthetic adapter through the real
`IssuerRegistry`; the current-host predicate is synthetic too. There is no
real domain approval, issuer nonce store, mounted human request, PostgreSQL
credential custody or service-restart/Redis rollback qualification here. Those
remain separately named gates. A source-only run with both targets unset runs
only fixture-safety tests and explicitly skips every backend recovery case.
