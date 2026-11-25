---
id: project-board.worker-reference.knowledge-keeper
title: The Knowledge Keeper Role
summary: How an optional knowledge keeper maintains the current concepts from milestone hand-overs and verifies what the knowledge base can say.
tags: [procedure, problem-board, worker, knowledge, role]
keywords: [knowledge keeper, knowledge role, knowledge base, hand-over, merged work, merge commit, source-only, merged, installed, observed, evidence level, send when unsure, keeper decides, retrieval check, superseded claim, consult before designing, project files, journal versus knowledge]
see_also:
  - journaling.md
  - coordinator.md
  - project-workspace.md
---

# The Knowledge Keeper Role

A project may define a **knowledge keeper**, an agent that writes and keeps
its knowledge base current from the team's milestone hand-overs. It is a role
like the coordinator's, optional per project (operator, 2026-10-04).

## The journal and the knowledge base are different things

The worker writes research insights in the [journal](journaling.md). The
keeper writes the **knowledge base** and updates each concept in place.
Explain the current architecture and behavior clearly, using diagrams where
they help. Include insights and limits that matter to understanding the part
today. Keep history, failed attempts and decisions specific to one ticket in
the journal. Keep the concept tree coherent and split a node when it grows
too broad.

The operator described the keeper's role on 2026-11-25.

> its the agent which is keeper. otehrs handoff in teh form which is enough for keepr to keep. keepr can ask for more why not

## What the project's own files say

Project Board names the role and its contract. Everything about a project's
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

Hand over a change that affects what the project's knowledge base covers.
This can include product behavior, terminology, flows, interfaces, data
models, supported workflows and operational facts.

- **At an agreed milestone.** Send the hand-over when the piece of work wraps
  up, including before merge. A reusable operational finding, consequential
  decision or correction can carry its own evidence without a code change.
  The keeper may ask for more.
- **Owning documentation.** When the change has an owning document, merge it
  with the change (collaboration, Rule 13). Link it from the hand-over.
- **Content and evidence.** Name the item and concept, what changed, how the
  part behaves at the cited source, what this corrects and where to verify it.
  Include an insight only if it helps explain the part as it stands now.
  Name the exact head and, when available, the merge commit. Label claims
  source-only, merged, installed or observed. The keeper can use a pre-merge
  hand-over to prepare the update and publishes current claims once the
  evidence supports them for the intended audience. Deployed behavior needs
  installed or observed evidence. The project's files own the detailed format.
- **On the item and by mail.** Record the hand-over as a note on the item and
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

Asked on 2026-11-25 whether a hand-over can go before merge, the operator
answered.

> yes can

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
