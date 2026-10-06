---
id: project-board.worker-reference.knowledge-keeper
title: The Knowledge Keeper Role
summary: A project may define an optional knowledge keeper, a role like the coordinator's that keeps the project's knowledge base current from finished work; when a hand-over is owed, what it carries, how the keeper's work is reviewed, and what stays in the project's own files.
tags: [procedure, problem-board, worker, knowledge, role]
keywords: [knowledge keeper, knowledge role, knowledge base, hand-over, merged work, merge commit, source-only, merged, installed, observed, evidence level, send when unsure, keeper decides, retrieval check, superseded claim, consult before designing, project files, journal versus knowledge]
see_also:
  - journaling.md
  - coordinator.md
  - project-workspace.md
---

# The Knowledge Keeper Role

A project may define a **knowledge keeper**: an agent that keeps the project's
knowledge base current from the work the team finishes. It is a role like the
coordinator's, optional per project, and the team uses it from time to time
when the project defines it (operator, 2026-10-04). A project that defines no
keeper reads nothing more here.

## The journal and the knowledge base are different things

The **journal** is the history of the work: what was decided and why, what was
rejected, what failed, what was learned. Those who do the work write it, and it
is never rewritten ([journaling](journaling.md)). The **knowledge base** says
how things work now, for a reader who never saw the work; the keeper maintains
it and corrects it in place. A hand-over carries what is now true from finished
work to the keeper; it changes no journal entry.

## What the project's own files say

Problem Board names the role and its contract. Everything about a project's
implementation lives in that project's files (instructions, facts,
environment) and in the knowledge package's own instructions, never in this
procedure:

- which agent holds the role, where the installed board does not carry it yet.
  Where it does, the board is the authority (`pb worker context` shows the
  role, and mail goes to `--recipient knowledge-keeper`), and the files only
  point to it. A project that declares the role without a holder says so:
  hand-overs then go to the coordinator, and nobody infers a holder from a
  repository or an alias. A
  holder who is unavailable is handled like any owner others wait on
  ([collaboration Rule 16](collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md)). The role grants no permission of
  its own;
- where the knowledge package and its instructions are, and the repositories it
  is built from;
- the hand-over's detailed shape and where hand-overs are kept;
- how the knowledge is served, queried and rebuilt, and on which machine;
- which kinds of change owe a hand-over in that project, beyond the rule below.

## When a hand-over is owed

When an item you completed changes something the project's knowledge covers
(product behaviour, terminology, flows, interfaces, data models, a supported
workflow, an operational fact), and the project has a keeper, hand it over:

- **After the merge.** The keeper records what is true on the integration ref,
  never intent: the hand-over names the item and the merge commit you fetched.
  A reusable operational finding, a consequential decision or a correction can
  owe a hand-over with no code merge; it names its own evidence.
- **Docs first.** When the change has an owning document, it is merged with the
  change (collaboration, Rule 13); the hand-over points to it rather than
  replacing it.
- **What it carries:** the item, the merge commit, what is now true, what it
  supersedes or corrects, the sources to link, and each claim's evidence level:
  source-only, merged, installed, or observed. A claim about deployed behaviour
  needs installed or observed evidence. The project's files give the detailed
  shape.
- **On the item and by mail:** record the hand-over as a note on the item and
  send it to the keeper by board mail naming that note, so it survives an unread
  inbox. Where the board carries the role, send it with
  `--recipient knowledge-keeper` **and** `--work-ref <the item>`: the board
  records a hand-over only for mail that names its item, and mail without one
  is delivered but never counted as pending.
- **When unsure, send it,** marked "keeper decides". Either way the item gets
  one line: the hand-over, or why none is owed; the keeper may overrule it. A
  hand-over that turns out unnecessary costs the keeper one look; a missing one
  leaves the knowledge silently stale.
- **It does not hold the work.** A pending hand-over never stalls a fix that is
  otherwise deliverable, unless the item's acceptance names the knowledge
  publication.

A change with no changed meaning (formatting, an internal refactor) owes
nothing; a test or refactor that changes an invariant others rely on is judged
by that effect, not by its label.

## The keeper's own work is ordinary work

The keeper turns hand-overs into rounds and runs them as any other work: an
item for the round (related hand-overs batched under one maintenance item, not
one item per message), an exact published change, a non-author review and a
non-author merge. A round names the hand-overs it consumed and, for each
changed claim, its source at an exact version; the originating item records
whether its hand-over is queued, incorporated, declined with the reason, or
waiting for evidence. Settling the hand-over message is receipt, not
incorporation: only the published round incorporates it. Where the board carries the
role, the keeper records that outcome on the hand-over itself with
`project.role.handover.decide` (incorporated with the round's result, declined
or needing evidence with the reason).

The reviewer checks the content against those sources and has a **separate
agent, with no maintenance context, ask one retrieval question per changed
concept**: the answer must rest on the new entries and state their documented
limits. The reviewer words at least one question itself, and a miss is a
finding reported with the query, not a pass. After a correction, it asks the old
question too and confirms the wrong answer is gone. The keeper's own retrieval
check does not replace this: an author's check cannot see its own overreach.

## Consult the knowledge before designing

When the project has a keeper, search its knowledge once for the subject before
designing or changing a contract, next to searching the plan and the journal
(the worker skill, Choose A Relevant Next Action). The project's instructions
say how. Note what it returned, or that nothing was relevant. The knowledge is
a starting point: a current operator ruling, the assigned item and the observed
source win over it, and a gap you find goes back to the keeper. Knowledge that
cannot be reached does not stall otherwise deliverable work.
