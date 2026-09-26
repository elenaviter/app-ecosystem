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

A project may later have a knowledge role that maintains a knowledge base from
the journal. It is being designed. Until the project has one, the journal
entry is the whole hand-over: write it so that nothing else is needed.
