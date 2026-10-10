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
- **The write check:** before every write and deletion under the Card store root, the operation must still own
  every Card key it holds. Otherwise it refuses `card_lock_lost`, or `card_lock_not_held` outside any operation.
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
- intent records (`card_participant.record`);
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

