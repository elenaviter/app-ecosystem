# Card version store (W661, piece 1)

How the Hub's Card store keeps a Card version for Problem Board's save: STAGE, PUBLISH and ROLLBACK
(contract v6.2 with EMain's K4 amendment and decisions D1-D5, 10 October 2026). Piece 2's
`card_version` endpoint calls it through `ServiceCardVersionStore` (`cards/card_version_port.py`).

Operator rules it follows: "each card has it data ONCE. others LINK"; "nothing should grow with the
cards"; "never nothing is being scanned"; the card id "NEVER changes"; an invitation "IS new card ...
UPSERT. overwrite"; "not yet final version does not work until theres final arrives".

## Objects, each read directly by name

| Object | Content |
| --- | --- |
| `<card>/current.json` | the one truth for "current": version, revision name, checksum |
| `<card>/revisions/<name>` | one version: the Card value plus `version_record` {txn, actor, at, catalog, binding} |
| `<card>/revisions/<name>.card-version.json` | while not yet final: `{txn}`, so the file is not history |
| `card-versions/<txn>.json` | the txn marker while the save is in progress: links only |
| `<card>/inflight.json` | the Hub-local operation (issuer update, lifecycle intent) in flight on the Card |

**The version file name** is a pure function of the link, the request time and the txn:
`card_revision_<utc_stamp(at)>_<version:08d>_<checksum[:12]>_<sha256(txn)[:12]>.json`. `at` is the save
request's own time, never the Hub's clock, so a retried STAGE names the same file. The txn tag makes one
file belong to exactly one txn: two saves with the same version, content and time never share a file
(B1). ROLLBACK finds a published version from STAGE's answer `{card, version, checksum}` plus `at` and
the txn, without listing anything. Older writers' names have no txn tag.

`version_record` is stored beside the Card value and stripped before the Card's content hash is
checked, so Card reads are unchanged.

## STAGE

Under every member Card's mutation lock (a flock, taken in sorted order), with every write drained
before the locks go:

1. txn, marker and binding checks; the ACTIVE catalog, read directly, must be PB's catalog
   (`stage_catalog_moved`);
2. per member: finalize the txn its `current.json` names (below); `base_version` must be current
   (`card_changed`); `base_version: null` is the invitation upsert over whatever exists;
3. the marker `staging`, naming every file this STAGE may write;
4. `prepare()` (piece 2's effect preparation);
5. the version files (sidecar first);
6. the marker `staged`.

**Read members** (`reads`, `{card, version}`; My Reset's Control C) are Cards a save reads but never
writes. They are locked in the same sorted order as the written members, a pending predecessor on them
refuses, and they must still be at `version` at STAGE and again at PUBLISH (`card_changed`). The
marker keeps only their links; they get no answer link and no effect.

A same-txn retry with the same request finishes the same files; a different request or binding
refuses (`stage_txn_conflict`, `txn_scope_mismatch`).

## PUBLISH

The base fence (`card_changed`), each member's `current.json`, the marker `published`, then each
effect once with its outcome recorded. Once every effect is recorded the marker is deleted: per save,
only the version itself remains. A `staging` marker refuses `txn_not_staged`; no marker refuses
`txn_unknown`.

## ROLLBACK

- marker `staging` or `staged`: if a `current.json` already names this txn's version, the txn is
  published (`already_published`); otherwise every effect is released, the files are deleted, then
  the marker (`rolled_back`);
- marker gone: the linked version files are read by name; files written by this txn mean
  `already_published`, otherwise `unknown_txn`. A link whose full checksum or version differs from the
  Card in that file is refused `card_version_link_mismatch` (the name carries only `checksum[:12]`).

ROLLBACK never deletes a published version.

## OUTCOME (contract v6.3, item 5)

A read-only answer to "what became of txn T", by the exact request PB persisted before PUBLISH (STAGE's
links plus `at`): `published` (the marker says so, or the marker is gone and the linked files carry T),
`staged`, `staging`, or `unknown_txn`. Per member it also says whether `current.json` names exactly T's
version. It takes the linked Cards' locks, writes nothing and lists nothing; another scope or caller is
refused `txn_scope_mismatch`.

## COMPENSATE (contract v6.3, item 7)

After a conclusively uncommitted PB outcome, PB asks the Hub to restore what each Card had before T:

- every linked Card's `current.json` must name EXACTLY T's version; otherwise (a successor) the answer is
  `compensation_superseded` and nothing is written, nothing rewinds;
- the earlier content is read by the link T's own version record keeps (`version_record.base`), never by
  listing, and published as a NEW version N+1 through the ordinary STAGE and PUBLISH of a deterministic
  compensation txn (`cmp-` + sha256(T)[:40]), so a retry answers `already_compensated` and writes nothing;
- a Card that is credential-bearing, or a version without a recorded base (written before v6.3, or a Card
  created on an absent id), is refused `compensation_unsupported`: the restore never guesses effects.
- the compensation txn is staged with the actor `{"subject": "connection-hub", "kind": "compensation"}`
  and the catalog label `compensation`, both recorded in its marker and version record; its request digest
  is sha256 over the canonical JSON of `{op, txn, links, at}` (with `at` the persisted `compensation_at`).

## Every writer finalizes what `current.json` names (D3)

Before any writer (STAGE, PUBLISH, any pointer write) builds on a Card, it reads `current.json`, the
version's record and that txn's marker. A `staged` marker whose versions are all current is set
`published` first and left for that txn's own PUBLISH retry or ROLLBACK to remove, so its ROLLBACK
answers `already_published`, with or without links. A partly current group is refused
`card_version_unresolved`. A predecessor whose effects are not all recorded refuses every other writer
(`card_version_effects_pending`); a writer never runs another txn's effects. Only that txn's own PUBLISH
retry or ROLLBACK runs them. Readers treat the version `current.json` names as final.

## Links held by other records (W661 scope B)

A record that has to name a Card version keeps a LINK, never a copy of the body:
`{card_revision, revision_name, content_hash}`. The record itself names the Card. The helper is
`cards/version_link.py`:
- `load_version` reads that one version file by name, strips `version_record`, and requires the full
  content hash, the access id and the revision to match. It is not a serving read: `read_revision`
  still hides staged files.
- A version that does not exist yet is written once by `write_hidden_version`, as its own txn-tagged
  file behind a `.card-transaction.json` marker that names its tag. `load_version(..., marker=tag)`
  then requires exactly that marker.

The OAuth issuance plan holds two links:
- the original: the committed version that `current.json` names;
- the candidate: written once behind `staging_tag("oauth-issuance-candidate", decision_request_id)`.
  The tag is `stg-` plus the SHA-256 of a length-prefixed encoding of scope and request id, so it is
  structurally outside the 64-hex transaction-id namespace. The store treats a `stg-` marker as never
  committed, so the file never becomes history. STAGE adopts that same file for the real transaction
  with `adopt_hidden_version`; only the marker moves.
- Before the candidate file is written, an attempt row (`connection_hub_oauth_issuance_plan_attempts`)
  records the planned clock and the file's link. The first planner's clock wins
  (`INSERT ... ON CONFLICT DO NOTHING`), so a retry, or a concurrent planner of the same request, builds
  the same candidate and names the same file, which is reused. An expired attempt is renewed only if it
  is still the one read, and its row is deleted once the plan is stored, for exactly that clock. No lock
  is held across these calls.
- A stored plan always wins. Another planner of the same request can finish the whole begin while this one
  is between two calls. Then this planner's pin reads no row, its candidate record updates no row, a
  stored plan is found after the pin, or its file is already adopted. In each case it writes no file and
  replays the stored plan instead (bounded rounds, then the retryable `issuance_plan_unavailable`).
- The original is read through the store's own committed read and checked against its link. A
  candidate with no marker is accepted only when the bound decision is COMMITTED in the decision log.
  `load_version` with no owner named refuses any marked file. Presence is decided in one read, so a
  concurrent writer is never mistaken for corruption.
- **Cleanup is disabled in this release: nothing is deleted.** `release_unbegun_oauth_issuance_plans`
  returns 0, and the reservation sweep does not call it. An outcome read still answers after an abort;
  plan reads and reserve answer `issuance_decision_closed` once the candidate is gone. A candidate file
  can stay unreferenced only after a crashed attempt whose Card base moved before its retry, or after an
  aborted decision. Such a file is hidden (a `stg-` or uncommitted-transaction marker), never history.
  A bounded cleanup on a per-request KDCube Redis lock, linearized with plan, begin, record and bind,
  is a separate follow-up.

A plan stored before this change still holds bodies and is read as before, until its decision finishes.

The one-off cutover step `cards/intent_purge.purge_finished_v1_intents` deletes v1 intent files. A
v1 intent is one that stores a Card body. It is deleted only when its decision is terminal and every
participant has finished. It is a dry run unless `apply=True`, and it reports transaction ids and
counts only. In-flight v1 intents stay until their transaction finishes.

## Hub transaction intent links and retirement

`LocalCardIntentSource` keeps single/group intent schema v2 at
`card-transactions/intents/<transaction_id>.json`. Each member contains its
Card coordinates and only the exact `original` and `candidate` links. An
absent creation has `original: null`. Card authorities are hydrated by exact
filename, full checksum, access id and revision only in memory; there is no
current/latest substitution or version-directory scan.

The same intent path first holds a `staging` manifest: every candidate name,
owner and the first accepted staging instant, before any candidate write.
Only a `ready` record can load for preparation; a partial manifest refuses
`card_intent_incomplete`. A replay resumes its exact files with the frozen
instant. The composed service's member Card sections, in sorted order with
started writes drained before release, cover recording and retirement.
Candidate sidecars are written before version bodies, so planned candidates
are not serving authority or history.

Pass the composed `DelegatedCardService` explicitly to the intent source in
production. For callers that omit `service=`, a bundle store retains its
original service as the fallback; constructing a different service on that
same store refuses `card_intent_service_conflict`, without replacing the
fallback or taking any Card lock. Even identical lock objects do not make two
services interchangeable. Separate stores can have separate services. An
explicit intent-source service still takes precedence over the fallback;
this registration guard adds no private lock, ledger or Card copy.

The W578 participant stages that SAME candidate file. A pre-begin OAuth plan
may supply `candidate_link` and `candidate_staging_tag` on `CardIntent` or on
each `CardGroupMemberIntent`: recording adopts the sidecar to the transaction
id (or its derived member id), never rewriting the body. These fields are
storage inputs; the kernel's in-memory candidate value and digests do not
change. Older unfinished v1 body intents remain readable and use their old
stage path.

FINISH authenticates the coordinator's terminal decision first. After every
service finish duty succeeds, the existing receipt or abort tombstone gains
bounded `intent_binding` (`authority`, `scope`, `record_digest`) and
`intent_finished: true`. Only then is the intent removed. An abort also
removes only its own exact unpublished candidate files and markers; it never
removes a current version. A failed finish or cleanup leaves retry evidence.
Terminal FINISH retries use the receipt without Card hydration; signed
retries and page cursors require its exact authority/scope binding. A legacy
receipt whose intent is gone and whose binding is absent fails closed.
Exact terminal record replay writes nothing; late STAGE is refused.

## Hub-local operations in flight

An issuer update or lifecycle intent writes `<card>/inflight.json` under the Card's lock before its
queue entry and clears it after it retires. A pointer writer reads that one file
(`issuer_update_preparation_unresolved`, `lifecycle_preparation_unresolved`) instead of listing the
queues. The queues remain for those flows' own crash recovery until the scope-B follow-up.

## The Card lock and who may write (option R, with phase-1 guards)

**The Card file-lock root.** It is the Hub bundle's own platform storage root plus `_card_locks`
(`bundle_storage_root()/_card_locks`; KDCube `sdk/bundle/bundle-storage-and-cache-README.md`: "use
self.bundle_storage_root() and create a subdirectory below it"). It is the shared local filesystem: local disk
here, EFS in the cloud. Each Card lock file is `<root>/_card_locks/<sha256 of the absolute Card lock path>.lock`.
There is no host path and no config root: phase 1's `/run/kdcube-card-locks` could not be created by the non-root
app user, and every Card write failed (10 October). No storage root refuses `card_lock_root_unavailable`; a root
the process cannot create refuses `card_lock_root_unwritable`.

**Option R** (operator, 10 October: "R now, with P"), selected by `delegated_credentials.lifecycle_storage.lock_backend:
"redis"`. It follows KDCube's own pattern (`service/synch-mechanisms/critical-section-README.md`, "Git Bundle
Materialization"): the per-key Redis lock first, THEN the observed file lock. The Redis lock is the SDK's
`observed_redis_lock_async`, reused unchanged:

- **The key:** `kdcube:cards:lock:<tenant>:<project>:card:<subject_hash>:<access_id>`; the owner goes in the value
  (lock metadata with a per-operation owner token). The service's other sections use `:lifecycle-txn:`,
  `:issuer-update-txn:`, `:account:` and `:collection:`, taken in the service's existing order.
- **Renewal:** an owner-checked Lua `PEXPIRE` every 20 s (TTL 120 s) in a task bound to the operation. A failure
  marks the operation lost.
- **The write check:** before every write and deletion under the Card store root made INSIDE an R operation,
  the operation must still own every Card key it holds and not be marked lost; otherwise it refuses
  `card_lock_lost`. A write outside any R operation carries no Card-lock claim (the proc-only guard applies). A
  task that outlived its operation is refused `card_lock_session_closed`.
- **Fail closed:** an unreachable Redis refuses `card_lock_unavailable` at once. A Redis whose
  `maxmemory-policy` is not `noeviction` refuses before any lock. A key held past the 30 s wait refuses
  `card_mutation_lock_timeout`.
- **The bounds stay ordered:** wait 30 s < PB lock_timeout 40 s < statement 45 s < idle-in-transaction 60 s <
  TTL 120 s.
- **One Redis node per deployment is assumed:** a replica promotion could grant a key twice.

**The residual.** A stall longer than the TTL between the owner check and a write lets one stale write land: an
expired lease can be reacquired while old code still acts (`sdk/bundle/bundle-scheduled-jobs-README.md`). KDCube's
data-bus guidance asks for a storage-level optimistic check in addition. That is P (the PostgreSQL version check),
which comes under R. TTL expiry is not a proof that the old process died.

**What R does NOT serialize.** R covers the Card mutation sections, the service's critical sections around a
Card or its txn, collection and account fence. Some writes under the Card store root happen outside any such section:
- read-set and effects-only intent records (single/group recording and retirement take their member Card locks);
- read-collection seal and sweep;
- agent shares;
- account-fence marks;
- effects-only receipts.
They carry no Card-lock claim; the owner check applies only to writes made inside an R operation. They rely on
their own rules (immutable exact-replay records, ids, receipts) and on the single-writer guard below, which is
therefore NOT retired by R.

**Phase-1 guards that stay.** Cards are written only in the chat-proc process role (`GATEWAY_COMPONENT=proc`).
Outside it, the Card lock, every write and every deletion refuse `card_store_write_wrong_process_role`. On the
Docker Desktop runtime the single-writer condition stays: exactly one proc-labelled container mounts the Card share
(the VM pause is exactly the residual above). It is retired only after R is qualified on a cluster deployment.


## The storage-level version check (option P, under the Card lock)

The operator's decision (10 Oct, about 21:50Z, verbatim: "R now, with P (the Postgres version check)"):
the Card lock serializes the normal case, and the storage refuses a stale writer. That is the KDCube
pattern in `service/comm/data-bus-README.md` ("Ordering And Concurrency": a short-lived Redis lock, and
"still require storage-level idempotency and optimistic concurrency checks"; "the storage layer must reject
stale updates"). `sdk/bundle/bundle-scheduled-jobs-README.md` says the same: an expired lease can be
reacquired while old code still acts, so durable effects need a resource-enforced guard.

- **Rows.** Each Card's pointer is a row in `connection_hub_card_pointers`, and each card-version txn
  marker is a row in `connection_hub_card_version_txns`, in the scope's schema (`kdcube_<tenant>_<project>`).
  Both hold the links-only document plus `row_revision`. Every change is a compare-and-swap on the
  `row_revision` this operation read: zero rows means a stale writer, refused `card_pointer_moved` or
  `card_version_marker_moved` whatever its lock state. `current.json` and the marker file stay as hints,
  written after the row.
- **Writer guards before the row.** Every row write (pointer, marker, delete, first-touch adoption) first passes
  the same writer guards as the file write it stands for: proc-only and, under R, the Card-lock owner check. A
  writer whose Redis key already expired is refused `card_lock_lost` before its CAS.
- **The row fence: an ordered claim, the latest lock owner owns the row.** The Redis check and the PostgreSQL
  CAS are separate resources, so a writer can pass its owner check, stall, and resume after its key expired and
  a successor took the lock (Infra's post-owner-guard case). So a read made by the row's lock holder CLAIMS the
  row in the same statement, and every CAS and delete of that operation requires the token it claimed with:
  - **The token.** One PostgreSQL sequence per scope schema (`connection_hub_card_row_fence`) hands out fence
    tokens. An operation draws its token BEFORE the owner guard that precedes every claim and write, and draws
    a new one after every further lock acquisition. A guard that passes after the draw proves the lock was
    still held then, so a successor on that lock acquires later and draws a LARGER token. The sequence lives
    with the rows it fences: no per-Card or per-operation key, nothing to seed after a Redis restore, and a
    database restore moves the rows and the sequence together. The sequence is `CACHE 1`, so `nextval` order
    is real-time order across connections (a cached sequence would break the fence).
  - **The claim** is one UPDATE that sets `fence_token` to t only where it is NULL or not newer, and returns
    whether it did (`fence_token = t AS claimed`). A stale operation's late claim (Ops' review of b1693251: an
    unordered per-operation id let it take the row back) carries the older token. The UPDATE still runs on the
    row, but it keeps the newer fence and leaves the document and revision as they are; the caller gets an
    unclaimed read. Its later CAS refuses, and the successor's CAS succeeds. Without ordering the live owner was refused instead (witness:
    `test_w661_p_row_late_claim_ordered_fence.py`, which fails on b1693251).
  - **Who claims.** A pointer row is claimed only by the holder of that Card's lock. A marker row is claimed
    by an operation inside any Card or txn section (every marker writer is; the txn's member Cards and its
    txn section all protect it). Two holders of different member Cards can both finalize one txn: the
    earlier token's claim is then an unclaimed read and its write refuses `card_version_marker_moved`, the
    same contention as the revision CAS alone. Any other read claims nothing.
  When the successor reads under its lock, it holds the claim, and the stalled writer's CAS then refuses
  (`card_pointer_moved` / `card_version_marker_moved`) with the row's document, revision and fence unchanged: the live lock owner wins, and nobody
  is told "failed" for a write that landed. A lock-free read claims nothing.
- **Residual: creates.** A row that does not exist yet has nothing to claim. If a stalled writer and its
  successor both observed "absent", the first INSERT wins and the other refuses (ON CONFLICT) and re-reads it.
  If the stalled one wins, its caller may see `card_lock_lost` from its file hint although its row landed: that
  outcome is in doubt, and is resolved from the row (the authority; v6.3 `outcome()` for card-version txns),
  never lost or duplicated.
- **Reads.** Once a row exists, every reader uses the row and never prefers the file, even a newer-looking
  one (C2). Without a row, the Card was not written since activation:
  - a writer holding that Card's lock adopts the file into the row (first touch, no scan);
  - a reader without the lock reads the file and creates nothing.
  - A marker file written while the rows are on carries `_card_rows_era: true`. A tagged hint with NO row is
    the late file write of a stale writer after the row was deleted: it reads as absent, is never adopted, and
    a reader holding the Card lock removes it (Infra's late-hint case). Only an untagged file, written before
    the cutover, is adopted. No per-txn tombstone row is kept.
  - A marker row can be deleted (ROLLBACK) and re-created for the same txn (re-STAGE), starting again at
    row_revision 1. So the marker CAS names the row's `incarnation` (random, set when the row is created) as
    well as its revision: a stale writer that observed the old row never matches the new one (Infra's ABA
    case). Pointer rows are never deleted (absence is a row with the ABSENT document), so their revisions only
    move forward.
- **The operation context.** `locks.row_fenced_lock` wraps whatever mutation lock serializes the normal
  case: the container-local flock (lock_backend local) or the per-Card KDCube Redis cluster lock
  (option R, lock_backend "redis"); `hub_card_mutation_lock` and `hub_redis_card_mutation_lock` both wrap
  their stack in it. It opens one `CardRowOperation` per Card operation. That context holds the revisions this
  operation read and the lock resources it holds; it has no connection and no lock of its own.
  - Each row statement runs on its own pool connection.
  - A write without a read of that row in the same operation is refused `card_row_unobserved`; a write
    outside any Card operation is refused `card_lock_not_held` by the rows (unlike R's file guard, which
    allows writes outside an R operation: no production pointer or marker writer runs outside the Card lock).
  - A child task that outlives its operation is refused `card_lock_session_closed`.
  - The lock-free precondition read (`current_revision`) checks D3 but never writes a marker; the writer's
    own commit, under the lock, marks it.
- **Activation (C1).** The bundle property
  `connections.delegated_credentials.lifecycle_storage.card_rows: "postgresql"` turns the rows on at bundle
  load. That records the scope's activation row. Set without a `pg_pool`, it refuses the load.
  - **One-way:** once a scope is activated, the Hub uses its rows even if the property is removed later.
  - **Cutover:** stop every Hub process of the scope, deploy, then start. A pre-P binary writes only the
    file, and new code cannot refuse it, so old and new processes must never overlap. The phase-1
    single-writer guard stays until R is qualified on a cluster deployment.
- **The txn's own section, first.** Every card-version operation (STAGE, PUBLISH, ROLLBACK) takes the txn's
  own section (`card-version-txn:<txn>`, R key kind `card-version-txn`) BEFORE the member Card sections, in that
  one order. So a ROLLBACK or PUBLISH that found no members yet still serializes with a same-txn STAGE that is
  creating them, and a txn with no member Card (e.g. a ROLLBACK under `KEEP_ROLLBACK_MARKER`) still writes its
  marker locked and row-fenced. (Before, such an operation held no Card lock and could act on a marker a
  concurrent STAGE had just written; the R+PG suite surfaced it as card_version_marker_moved.)
  Its local lock file is one of 256 fixed shards chosen by the txn's digest (`txn_section_lock_path`), so late
  rollbacks never grow the lock root; under R the per-txn Redis key expires with its TTL.
- **Process state.** `_CARD_ROWS_BY_ROOT` is a registry of ACTIVATED SCOPES (a storage root to its row
  authority), not a cache of Card data: it holds no document, and every read goes to PostgreSQL. The scope's rows are registered per Card storage root at `on_bundle_load`, which every proc
  runs process-locally before admission; an entrypoint instance is not guaranteed to be a singleton, so the
  rows are not held on it. A scope where `card_rows` is not set and was never activated gets no DDL.
- **Evidence tooling.** `W661_BCORE_PG_MODE=1` re-runs the Hub package suite against the rows;
  `W661_PG_AUDIT=<file>` records every pointer or marker write made outside a Card row operation with its entry
  frame (a production writer there would be refused `card_lock_not_held`).
- **Residual without P.** The Redis lock (R) alone leaves one stall window: a holder paused past the
  lock's TTL can still write once. With P that write is refused by the row; the test
  `test_a_holder_whose_lock_expired_cannot_write_after_another_writer_published` pins it.
