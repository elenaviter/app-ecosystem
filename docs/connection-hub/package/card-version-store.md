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

## The Card lock and who may write (K1, phase 1)

K1 on the live Docker Desktop host (10 October) showed that `flock` on the shared `/bundle-storage` bind
is not exclusive between processes (1 overlap in 600). Phase 1 therefore:

- takes the Card mutation lock (the SDK `flock`) on a **container-local** file, one per Card, under
  `delegated_credentials.lifecycle_storage.lock_root` (default `/run/kdcube-card-locks`); the Card files
  stay on bundle storage;
- writes Cards **only in the chat-proc process role** (`GATEWAY_COMPONENT=proc`, set by the platform).
  Outside it the Card lock, every JSON write under the Card store root and every deletion there refuse
  `card_store_write_wrong_process_role`; the legacy repair on bundle load is skipped outside proc.

This holds while exactly ONE container both runs in role `proc` and mounts the Card share. `proc` is not
unique to chat-proc (the platform also labels metrics `proc`), so the condition is about every
proc-labelled container that can reach the Card root: on the live host (10 October) only chat-proc
mounts `/bundle-storage`; metrics does not. A second such container needs the PostgreSQL lock. PostgreSQL advisory locks with a
session-held check replace it before W661 is Done.

