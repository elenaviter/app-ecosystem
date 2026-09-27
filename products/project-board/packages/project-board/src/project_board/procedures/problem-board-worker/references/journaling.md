---
id: project-board.worker-reference.journaling
title: Journal Every Completed Move
summary: When a worker or coordinator writes a project journal entry, what it carries so a later reader can act on it, and where each kind of knowledge goes.
tags: [procedure, problem-board, worker, coordinator, journal, knowledge]
keywords: [journal entry, completed move, one entry per move, project journal, pb worker context, local_journal_directory, pb worker journal-index, pb worker journal-search, operator ruling verbatim, where knowledge goes, private memory, procedure change request, knowledge role]
see_also:
  - coordinator.md
  - collaboration.md
  - project-workspace.md
---

# Journal Every Completed Move

The project journal is the team's shared record: what the project decided,
what shipped, what broke and why. An agent that finishes a move and writes
nothing leaves the next reader (a teammate, a successor coordinator, itself
after a compaction) to rebuild it from mail. How an entry is authored and
indexed (front matter, `entry_ref`, `journal-index`) is in the skill's section
*Work, Report, And Journal*; this page says when to write one and what goes
where. The entry is written in, indexed from, and searched in your own clone
of the journal repository, never a host-wide checkout
([project-workspace](project-workspace.md), step 5).

## When to write an entry

Write one entry for each completed move, as the move completes:

- a change request merged, with what it changes and the merge commit;
- a decision taken, an operator ruling above all, with its exact words;
- a runtime window's outcome: what loaded, what was verified, what failed;
- a gap found: the symptom, the cause when known, and who owns the fix;
- an assignment completed, with what the reviewer can check.

A move is complete when its result is on the board or in a merged commit, not
when the work started. One entry per move; a long item gets several.

## What an entry carries

An entry is read cold, by someone who was not in the conversation. It carries:

- **what happened**, in one or two sentences, first;
- **the refs**: the work item, the change request and merge commit, the mail
  or note it answers, the deployed commit;
- **the exact words** of an operator ruling, quoted, with its time;
- **the failure text**, verbatim, when something failed;
- **the verification**: what was run or checked, and its result;
- **what is still open**, and who holds it.

Leave out what nobody will need. Do not invent sections to fill a template.

## Where knowledge goes

| Knowledge | Where it goes |
|---|---|
| Project state: what shipped, what is open, what failed | The project journal, one entry per move |
| An operator ruling, with its reason | The project journal, and a note on the item it decides |
| A practice useful to any coordinator or worker, on any project | This procedure, through a change request |
| A behaviour of Problem Board itself | The public documentation, in the same change as the code (collaboration, rule 13) |
| A personal preference of one agent's user | That agent's private memory, and nothing else |

Knowledge kept only in one agent's private memory is lost to every other
agent and to that agent's successor.

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
the team finishes. The journal says what happened; the knowledge base says
how things work now, for readers who never saw the work. The operator's
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

The journal entry for the move comes first; the hand-over points to it.
