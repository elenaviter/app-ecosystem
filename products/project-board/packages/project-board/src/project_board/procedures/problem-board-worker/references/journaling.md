---
id: project-board.worker-reference.journaling
title: Journal Every Completed Move
summary: When a worker or coordinator writes a project journal entry, what the entry carries so a cold reader can act on it, where each kind of knowledge goes, and the optional hand-over to the project's knowledge keeper.
tags: [procedure, problem-board, worker, coordinator, journal, knowledge]
keywords: [journal entry, completed move, one entry per move, project journal, pb worker context, local_journal_directory, pb worker journal-search, pb worker journal-index, append-only, correction entry, operator ruling verbatim, where knowledge goes, private memory, procedure change request, knowledge keeper, knowledge wiki, hand-over, handover, six parts, retrieval summary]
see_also:
  - coordinator.md
  - collaboration.md
  - project-workspace.md
---

# Journal Every Completed Move

Read this when a move completes and you are about to write it down, and when
you decide where something you learned belongs. The journal is how the story
of the work survives a compacted context, a restarted session and a handoff
to another agent. An agent that worked all day and wrote no entry has left
work the next session cannot explain.

The mechanics of an entry (front matter, `entry_ref`, the UTC stamp,
`pb worker journal-index` and its recovery) are in the skill,
the skill's section *Work, Report, And Journal*. What the
journal home holds for the whole team (facts page, environment page, rulings,
window outcomes) is in the coordinator reference,
[Keep everything known in the project journal](coordinator.md#keep-everything-known-in-the-project-journal).
This page does not repeat either.

## Where the entry goes

The project journal is the one the board names for the project you attend.

```bash
pb worker context --project-ref <project-ref> --format brief
```

- `local_journal_directory` is where you write the entry, inside the journal
  repository's clone ([project workspace](project-workspace.md), step 2).
- After writing, index it with `pb worker journal-index` as the skill says.
- Find entries with `pb worker journal-search --query <subject>` (add
  `--project-ref` when you attend several projects). Search before acting on
  a subject, and read what it returns.

A journal entry is committed and exchanged like any other change
([collaboration](collaboration.md), Rule 2). It is never a local-only file.

## When to write an entry

One entry per completed move, written when the move completes, in the same
session. Do not batch a day into one entry at the end. A move is complete when
its outcome is known:

- **a merge**: the change request merged, written after the merge commit is
  fetched, never from the intention to merge;
- **a decision**: a design decision reached or reversed, an operator ruling,
  a coordinator routing or ownership decision;
- **a correction or veto**: the operator or a reviewer said a shape is wrong.
  These are the most valuable entries;
- **a runtime window outcome**: what loaded, at which commit, what was proven,
  what stays pending;
- **a found gap**: a missing rule, a product defect, a missing environment
  step, with its evidence;
- **an incident**: something broke, with the diagnosis;
- **a hand-over**: produced or received, the knowledge-keeper hand-over
  included.

Mechanical edits, renames and formatting passes get no entry. They ride the
next real one.

## What an entry carries

A cold reader must be able to reconstruct the move without the conversation.

- **Attribution**: the logical user the work is for, the logical machine, the
  agent provider (`codex`, `claude-code`, or another client) and this session's
  native resumable session id, read from the session itself. Stable logical
  names only: never an email address, a raw hostname, an IP address, a
  credential or a token.
- **Intention**: the need the move served, in the operator's terms, and why
  now. Not the item title.
- **What changed and why**: the driving problem, then the mechanism, with the
  evidence that proved it (the log line, the failing assertion, the counts).
- **The operator's words verbatim** for a ruling, a correction or a veto. A
  paraphrase loses the force, and the quote settles later arguments.
- **What was deliberately not done**: vetoed paths and who vetoed them,
  failures left untouched, scope deferred. Silence reads as "not considered".
- **Sources**: what you read before your model changed, so the next agent does
  not repeat the reading.
- **Pitfalls and insights**: the traps met or narrowly missed, and what is
  understood now that was not before.
- **The right shape**: the contract as it now stands, stated positively.
- **Hard references**: the item ref, commits, the merge commit, test counts
  before and after, the runtime and ref a window released.
- **Pending and next action**: what is implemented but not yet proven, as
  concrete checks, each with the artifact it concerns.

Every claim names the artifact it rests on: a `repo:<repository>/<path>`
reference, a commit, an item ref, a mail ref. Prefer the exact file over the
directory and the exact symbol or section over the file. A next action without
a reference is a note to someone who already knows, and later nobody does.

## Principles

1. **Append-only.** Never rewrite or delete a past entry. When later work
   invalidates one, write a new entry that names it and says it is superseded.
   The wrong turn is what stops the next agent from taking it again.
2. **A correction is its own entry**: the wrong shape, the right shape, and
   the ownership rule that decides between them.
3. **An incident carries evidence**: the observable, the mechanism and the
   fix, so the failure class is kept, not only the fix.
4. **Write for the next agent**: no conversation shorthand, codenames spelled
   out on first use, full references.
5. **The journal feeds, it is not fed.** Hand-overs, articles and product docs
   are written from the journal after the fact. A published page is never the
   record.

## Where knowledge goes

| What you learned | Where it goes | How |
| --- | --- | --- |
| Project state: facts, decisions, operator rulings, window outcomes, gaps found | The project journal | An entry, indexed; the facts or environment page updated when a standing fact changed |
| A practice useful to any coordinator or worker, on any project | This procedure package | A change request against the owning rule (the skill's section *Review Foundations And Procedure Gaps*) |
| A personal preference of this agent | Private agent memory | Only this; nothing the team or a successor needs |

Private memory is invisible to every other agent and to the next holder of
your role. A rule kept there is lost at the next handoff, and a project fact
kept there is missing from every search. When in doubt, it is team knowledge.

## The knowledge keeper, an optional role

A project may have a knowledge keeper: an agent on the team that maintains the
project's knowledge wiki (entries, aliases, ontology, search index) from the
hand-overs other agents send it. The knowledge keeper already knows how to
structure the wiki. A hand-over tells it what changed and what must be
represented, never how to index.

The role is optional. A project without a knowledge keeper works the same way:
the hand-over is skipped, and the journal entry is sufficient. When you cannot
tell from the project's team whether one exists, ask the coordinator once.

### When a hand-over is due

Write one after a move that changes what a later agent should get back from a
knowledge search:

- a feature or concept was added or its meaning changed;
- a fix corrected a semantic, not only a behaviour;
- a product misunderstanding was resolved and is likely to recur outside this
  work;
- a new term needs an entry or an alias.

An open hypothesis is not handed over as a rule. Write the journal entry
first; the hand-over is written from it.

### The six parts

1. **New or changed features and concepts.**
2. **Fixes and semantic corrections.**
3. **Terms** that need wiki or ontology entries, or aliases.
4. **Do not misunderstand this**: the readings a later agent is likely to get
   wrong, with the resolved correction.
5. **Sources to read and link**: the docs and source files, by full
   `repo:<repository>/<path>` reference, and the journal entry.
6. **Retrieval summary**: a short summary of what an agent should later get
   back from a knowledge search on this subject.

### Sending it

Send the hand-over as board mail to the knowledge keeper's stable worker name,
with the body in a file (`--body-file`) and the work item ref it belongs to.
`pb worker send --help` is the argument contract. The hand-over is itself a
move: note it in the journal with the mail ref, so the next agent sees that
the knowledge keeper already has it.
