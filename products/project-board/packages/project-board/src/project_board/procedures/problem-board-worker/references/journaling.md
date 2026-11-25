---
id: project-board.worker-reference.journaling
title: Journal What The Project Learned
summary: How workers preserve actionable research records at agreed milestones, group them by feature and month, and use them before retrying an approach.
tags: [procedure, problem-board, worker, coordinator, journal, knowledge]
keywords: [journal entry, project wisdom, why this and not that, rejected alternatives, failure mechanism, limits and gaps, project journal, pb worker context, local_journal_directory, pb worker journal-index, pb worker journal-search, operator ruling verbatim, where knowledge goes, private memory, procedure change request, knowledge role]
see_also:
  - coordinator.md
  - collaboration.md
  - project-workspace.md
---

# Journal What The Project Learned

The journal is the project's research protocol. The worker writes actionable
records that let a successor reconstruct the reasoning behind a complex
feature or decision and continue from what the team learned. The project
may declare role `journal` for any repository and path. The common
procedure never assumes a fixed home. With no journal role, there is no
journal work. Keep the evidence the team needs on the item.

Author an entry in the current work item's worktree at the configured journal
path. Reuse the item's existing worktree when it already changes the journal
repository; otherwise make `<workspace>/wt/<item>-<journal-alias>` on a
feature-bound branch and open a change request for the item. Never carry a
long-lived per-agent journal worktree or branch across unrelated items. After
the change request is merged, fast-forward your clean clone and index and
search the merged entry there, never from a host-wide checkout
([project-workspace](project-workspace.md), steps 5 and 6).

## Write at milestones

Write records only at milestones, when an agreed piece of work wraps up.
A milestone is a point the person and worker agree on. It often comes as a
change approaches merge and can also come partway through a piece of work.
Between milestones, keep working notes and relevant mail so you can distill
the evidence at the next milestone. Link the resulting records from the item.

The operator described an agreed milestone on 2026-11-25.

> something that the user and worker agrees. usually the sign is that something is going to be merged or something like that

The operator also set the cadence on 2026-11-25.

> we only do this when we wrap up. on milestones

## What a record carries

Lead with the lesson a successor can act on. Include only what would change
how they approach a complex feature or decision. Leave routine steps and
small decisions in working notes.

- Describe what was tried, the result and why it happened. Include the
  relevant evidence, such as a failure, measurement or source reference.
- For a failure, say whether the approach **does not work at all** or
  **did not work then, because of stated conditions**. State the conditions,
  such as the data, version, scale or missing dependency, and what would
  make a retry worthwhile. Repeat an earlier attempt only when the relevant
  conditions changed. Say what changed and link the earlier record.
- Record the corrections, decisions that shaped the work, alternatives and
  reasons, and any limits that matter to the next worker. Quote any ruling
  that shaped the decision verbatim with its date and source.

## Keep one running entry per feature per month

Append milestone records to the feature's running entry for the current
month. Start a new entry for a feature with the first record of a new month.
Append a correction that identifies the earlier record and links the evidence
that corrected it. Earlier records remain intact. Group a milestone's journal
records into one change request.

W1031 tracks deliberate re-indexing of amended entries, the append command
and draft search. Until that support ships, author records through the current
item worktree and index the merged entry from the clean clone. For an entry
already indexed, follow the current superseding-entry path if an amendment
would conflict. Carry the earlier records and link their original entry.

## Where knowledge goes

| Knowledge | Where it goes |
|---|---|
| Why the project chose this and not that, failures and their mechanisms, wrong assumptions, limits and gaps learned in the work | The project journal |
| Progress, heads, approvals, gates, merges, installs: where the work stands | The work item's notes and reports |
| An operator ruling, with its reason | The project's facts file while it is in force, a note on the item it decides, and the journal entry that explains what it changed |
| A practice useful to any coordinator or worker, on any project | This procedure, through a change request |
| A practice or fact of one project: its machines, conventions, how it tests and deploys | That project's files (instructions, facts, environment), which every agent there reads |
| A behaviour of Project Board itself | The public documentation, in the same change as the code (collaboration, rule 13) |
| Current behavior, architecture and relevant limits of a feature | The owning documentation and, when configured, the knowledge base node. The journal records the reasoning and links the evidence |
| A unique finding from a scratch run (a review result, a probe's outcome) | An applied note on the item, or a tracked file, before the run is closed; the run records that reference ([project workspace](project-workspace.md), section 6) |
| A personal preference of one agent's user | That agent's private memory, and nothing else |

Knowledge kept only in one agent's private memory is lost to every other
agent and to that agent's successor. A finding about how the team works, or
about Project Board itself, is never private memory: it becomes a procedure
or documentation change so that every agent follows it, and a project's own
practice becomes a change to that project's files (operator, 2026-10-01).

## Write and index an entry

- Author the journal Markdown at the configured relative path in the current
  item's worktree. Include the front matter when starting an entry. It needs a unique
  `work:journal:<created-at>:<entry-id>:<semantic-name>` `entry_ref` (semantic
  name at most 64 characters of `a-z0-9-`, else `journal_entry_ref_invalid`)
  and this exact `project_ref`; `title`, `summary`, `keywords`, `see_also`,
  status and attribution make retrieval better. The filename stamp and
  `<created-at>` both name `recorded_at` in UTC (`date -u`, never `date`, a test
  enforces it): [collaboration](collaboration.md), finding ten.
- After the journal change request is merged, fetch and fast-forward the clean journal clone, then run `pb worker journal-index --project-ref ... --repository-journal-ref ...`; an unmerged entry correctly returns `journal_entry_not_found`. The command does not rewrite the file and returns its index, validation and receipt steps.
  After interruption, inspect with `pb worker journal-index-status --project-ref ... --operation-id ...`, then run `pb worker journal-index-resume --operation-id ...` for the first incomplete step. Status is observation only: it does not rebuild, enqueue, or repair. Do not rerun the original command to guess what happened.
  For a pre-ledger validation use `journal-index-status --project-ref ... --outbox-id ... --repository-journal-ref ...`; it distinguishes an accepted plan revision from an absent receipt.
  Search with `pb worker journal-search --query ...` (`--project-ref ...` to name the project explicitly); legacy files remain searchable under a path-derived identity and status names compatibility issues.

## Finding what the project already knows

Before trying an approach, read the relevant knowledge base node when the
project has one. Use it to answer how the part works. Search the journal for
what was tried, what failed and why, and use the record to explain the choice.

```bash
pb worker journal-search --query '<subject>'
```

Read what it returns before deciding; a ruling found there binds as much as
one in your inbox.

## The knowledge keeper

At the same milestone, hand over what the knowledge base needs when the
project has a keeper. The worker writes the research records. Include an
insight in the hand-over only where it helps someone understand the part
as it stands now. Link the record and the relevant knowledge concept from
the item, and use those links when briefing a successor. See [knowledge keeper](knowledge-keeper.md).
