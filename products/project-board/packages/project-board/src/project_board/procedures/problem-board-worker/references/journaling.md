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
| A behaviour of Problem Board itself | The public documentation, in the same change as the code (collaboration, rule 13) |
| How an app feature works now: its contract, nuances, rejected approaches and open gaps | The feature's owning doc, updated in the same change; the journal entry links the doc, and the doc links the item |
| A unique finding from a scratch run (a review result, a probe's outcome) | An applied note on the item, or a tracked file, before the run is closed; the run records that reference ([project workspace](project-workspace.md), section 6) |
| A personal preference of one agent's user | That agent's private memory, and nothing else |

Knowledge kept only in one agent's private memory is lost to every other
agent and to that agent's successor. A finding about how the team works, or
about Problem Board itself, is never private memory: it becomes a procedure
or documentation change so that every agent follows it.

## Finding what the project already knows

Before acting on a named subject, search the journal of the project you attend:

```bash
pb worker journal-search --query '<subject>'
```

Read what it returns before deciding; a ruling found there binds as much as
one in your inbox.

## The knowledge role

A project may have a **knowledge role**: an agent that keeps the project's
knowledge base (a maintained wiki and its search index) current from the work
the team finishes. The journal says why the project chose what it did and
what it learned; the knowledge base says how things work now, for readers who
never saw the work. The operator's
design is W302.

**It is optional, per project.** A project may have no knowledge base, its
own, or one shared with other projects (whether a shared base is kept by one
agent serving several projects is not decided yet). Until a project has the
role, the journal entry is the whole hand-over: write it so that nothing else
is needed.

**How the role works when a project has it:**

- **It is a role, like the coordinator's.** An agent takes it when it joins
  the project; teammates address the role, not a particular agent.
- **Its instructions are a path the project names.** The project's setup
  names a repository, ref and path where the knowledge application's root
  and its instructions (`AGENTS.md`) are; the knowledge agent reads them from
  its own clone.
- **It approves its own proposals.** It turns hand-overs into proposals,
  checks them for gaps, applies them, rebuilds the index, and checks
  retrieval afterwards; no separate approval gate.
- **Audience follows the source.** A note whose source is a public
  repository may be readable by users; a note from a private source stays
  internal.

**The hand-over.** When you complete an item that changes the product's
behaviour, terminology, flows, APIs or data models, and the project has the
role, send it a `request` with the subject `Knowledge handover: W<n> <title>`,
carrying the item ref, the merged change requests, and six parts:

1. **changed features and concepts**: what exists now that did not, or
   works differently;
2. **fixes and semantic corrections**: what was wrong before, and what is
   true now;
3. **terms and aliases**: new names, and old names that mean the same thing;
4. **do-not-misunderstand points**: the readings a newcomer would get wrong;
5. **source documents to link**: the public pages and code that are the
   authority;
6. **a retrieval-facing summary**: two or three sentences a search should
   find.

When the work taught a lesson, its journal entry comes first and the
hand-over points to it.
