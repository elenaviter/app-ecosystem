---
id: project-board.worker-reference.journaling
title: Journal What The Project Learned
summary: What the project journal is for (the reasoning, failures, rejected alternatives and limits a later agent needs and no document holds), when an entry is worth writing, what it carries, and where every other kind of knowledge goes.
tags: [procedure, problem-board, worker, coordinator, journal, knowledge]
keywords: [journal entry, project wisdom, why this and not that, rejected alternatives, failure mechanism, limits and gaps, project journal, pb worker context, local_journal_directory, pb worker journal-index, pb worker journal-search, operator ruling verbatim, where knowledge goes, private memory, procedure change request, knowledge role]
see_also:
  - coordinator.md
  - collaboration.md
  - project-workspace.md
---

# Journal What The Project Learned

The project journal accumulates what the team learned while working on the
project and cannot keep anywhere else: why it chose this and not that, what
failed and through which mechanism, which assumptions turned out wrong, and
the nuances, limits and open gaps of a feature. A later agent who takes up a
similar feature searches it to understand the reasoning and not repeat the
team's mistakes. Documentation says how things work now; the work item says
where the work stands; the journal says why, and what not to try again
(operator, 2026-10-01: "its for systematic accumulating of the knowledge,
wisdom while wokring on the project. something that cannot be included in the
documentation. something which improves later the understandgin why we did
this and not that way not to repeat the mistakes we made").

How an entry is authored and indexed (front matter, `entry_ref`,
`journal-index`) is in the skill's section *Work, Report, And Journal*; this
page says when an entry is worth writing, what it carries and where other
knowledge goes. A project journal is optional. When the project declares a
repository with role `journal`, that role may name any repository and path;
the common procedure never assumes a fixed home. With no journal role, there
is no journal work: keep decisions and findings in the plan item.

Author an entry in the current work item's worktree at the configured journal
path. Reuse the item's existing worktree when it already changes the journal
repository; otherwise make `<workspace>/wt/<item>-<journal-alias>` on a
feature-bound branch and open a change request for the item. Never carry a
long-lived per-agent journal worktree or branch across unrelated items. After
the change request is merged, fast-forward your clean clone and index and
search the merged entry there, never from a host-wide checkout
([project-workspace](project-workspace.md), steps 5 and 6).

## When an entry is worth writing

Write an entry when the work taught the project something a later agent
needs and the code, the documentation and the item will not tell it:

- a decision between real alternatives, with why the others were rejected;
- a failure, and the mechanism that produced it, not only its symptom;
- an assumption that turned out wrong, and how it showed;
- an operator ruling, quoted with its time and its reason;
- a limit or open gap the change leaves, and what would close it.

A status, a head, a merge, an install or an approval is not a reason for an
entry: progress and the release ledger live on the work item, in its notes and
reports. A long item may need several entries or one; a routine item may need
none. Indexing comes after the journal change is merged and the clean clone is
fast-forwarded. An unmerged entry is intentionally absent from that clone, so
`journal_entry_not_found` before merge is not an indexing failure.

## What an entry carries

An entry is read cold, by an agent who was not in the conversation and may
come months later to a similar feature. Lead with the lesson, then give what
makes it trustworthy and findable:

- **the lesson**, first: what was learned, in a sentence or two;
- **the mechanism**: why it happened, with the code path, rule or condition
  that produced it, and how to recognise it again;
- **the alternatives**: what was tried or considered, and why each was
  rejected;
- **the operator's exact words**, quoted with the time, when a ruling decided it;
- **selective evidence**: the failure text, measurement or probe that proves
  the point, not every gate that ran;
- **the limits and open gaps**, and where the rule or feature now lives (the
  owning doc or procedure section, the item).

Keep commit and change-request refs to what a reader needs to find the
evidence. Leave out what nobody will need, and do not invent sections to fill
a template: a short entry with one real lesson beats a long checkpoint.

## Where knowledge goes

| Knowledge | Where it goes |
|---|---|
| Why the project chose this and not that, failures and their mechanisms, wrong assumptions, limits and gaps learned in the work | The project journal |
| Progress, heads, approvals, gates, merges, installs: where the work stands | The work item's notes and reports |
| An operator ruling, with its reason | The project's facts file while it is in force, a note on the item it decides, and the journal entry that explains what it changed |
| A practice useful to any coordinator or worker, on any project | This procedure, through a change request |
| A practice or fact of one project: its machines, conventions, how it tests and deploys | That project's files (instructions, facts, environment), which every agent there reads |
| A behaviour of Problem Board itself | The public documentation, in the same change as the code (collaboration, rule 13) |
| How an app feature works now: its contract, nuances, rejected approaches and open gaps | The feature's owning doc, as they stand now, updated in the same change; why, and the history of what was tried, go to the journal; the journal entry links the doc, and the doc links the item |
| A unique finding from a scratch run (a review result, a probe's outcome) | An applied note on the item, or a tracked file, before the run is closed; the run records that reference ([project workspace](project-workspace.md), section 6) |
| A personal preference of one agent's user | That agent's private memory, and nothing else |

Knowledge kept only in one agent's private memory is lost to every other
agent and to that agent's successor. A finding about how the team works, or
about Problem Board itself, is never private memory: it becomes a procedure
or documentation change so that every agent follows it, and a project's own
practice becomes a change to that project's files (operator, 2026-10-01).

## Finding what the project already knows

Before acting on a named subject, search the journal of the project you attend:

```bash
pb worker journal-search --query '<subject>'
```

Read what it returns before deciding; a ruling found there binds as much as
one in your inbox.

## The knowledge keeper

A project may define a **knowledge keeper**: a role of its own, like the
coordinator's, that keeps the project's knowledge base current from finished
work. The knowledge base says how things work now; this journal stays the
history of the work, and neither replaces the other. When the project has a
keeper, finished work that changes what its knowledge covers also owes a
hand-over: [knowledge keeper](knowledge-keeper.md).
