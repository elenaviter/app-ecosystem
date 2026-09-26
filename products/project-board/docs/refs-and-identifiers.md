---
id: project-board-refs-and-identifiers
title: Refs And Identifiers
summary: How Problem Board names its objects (work:... references), the stable plan-node identity versus an exact item version, what makes a new version, the limits, and the references other systems own.
tags:
  - project-board
  - references
  - identifiers
keywords:
  - work ref
  - identity_ref
  - versioned_work_ref
  - work_version_ref
  - item_key
  - plan node
  - work_plan_item_version_stale
  - repo ref
see_also:
  - ./README.md
  - ./concepts.md
  - ./projects-runtimes-and-refs.md
---

# Refs And Identifiers

Everything Problem Board keeps has a **reference**: plan items, assignments,
controls, mail, events, journal entries, notes, reports. A reference is text
an agent copies whole and never builds itself: commands print the ref to
copy (`project.plan.item` prints `identity_ref`), and a ref assembled from a
key and a title is refused.

## The shape

```text
work:<kind>[:<subkind>]:<created-at>:<key>:<semantic-name>
```

- **`created-at`** is the UTC time the object was created
  (`YYYYMMDDTHHMMSSZ`), so references sort by age.
- **`key`** is the object's stable lookup key: lowercase letters, digits and
  hyphens, at most 128 characters, never truncated.
- **`semantic-name`** is a readable slug of the object's title when it was
  created. It never changes afterwards: renaming an item does not change its
  reference.

Three kinds are deliberately shorter: a project, `work:project:<project-id>`;
a worker, `work:worker:<runtime>:<native-session-id>` (the address is the
runtime and session, never an alias); and a mail lease,
`work:lease:<lease-id>`, a short-lived handle for settling one message.
Dependencies between plan items have no reference of their own: they are
edges between item identities.

The kinds: `plan` (subkind `node` for an item), `assignment`, `control`,
`mail`, `inbox`, `event`, `journal`, `journal_view`, `note`, `note_view`,
`report`, `session_resume` and `call`. An unknown kind or a malformed segment
is refused, and a reference must resolve to a real object.

## A plan item: its identity and its versions

A plan item has two references:

| Reference | Segments | Means |
| --- | --- | --- |
| `identity_ref` | `work:plan:node:<created-at>:<key>:<semantic-name>` | The item, whatever its current state. |
| exact ref | the identity plus `:<version-timestamp>-<version-slug>` | The item exactly as it was at one authoritative change. |

The version timestamp is the time of that change; the slug is a SHA-256 of the
item's normalised state, its revision and that timestamp. A complete item
reference is at most 512 bytes.

Where each is used:

- `depends_on[]` names identities: a dependency is on the item, not on one of
  its states.
- A read of an item (`project.plan.item`) returns `identity_ref` and the exact
  ref as `item_ref`. A read of an assignment returns `identity_ref` and the
  exact version that was assigned as `work_ref` (also as
  `versioned_work_ref`).
- The `assign` notice an agent receives carries the stable identity as
  `payload.work_ref`. A report (`pb worker report`) carries no work ref at
  all: its `assignment_ref` and `ownership_version` name the assignment.
- An item's short key (`W343`) addresses the current item inside its project;
  see [item keys](concepts.md#item-keys-and-files-on-an-item).

**A stale exact ref is refused, never upgraded.** A mutation that names an
exact ref older than the item's current version is refused with
`work_plan_item_version_stale`, which returns both the identity and the
current exact ref. Read the item again, then decide.

**What makes a new version.** A change to the item's identity or key, title,
order, tags, keywords, status, assignee, start or cancellation, dependencies,
number of notes or files, or body. Not: reads, search and embedding data,
leases and receipts, wakes, or an edit that normalises to no change, which
keeps the revision.

## References other systems own

- **`repo:<alias>/<path>`** names a file or folder in one of the project's
  repositories, by the alias on the project card. Each worker resolves it in
  its own clone, `<workspace>/<alias>`; it never names a host path. See
  [projects, runtimes and refs](projects-runtimes-and-refs.md).
- **Connection Hub** Card and grant identifiers belong to Connection Hub.
- **Commits, pull requests, result refs and attachment refs** are opaque to
  the board: stored and shown as given, never turned into a path.
