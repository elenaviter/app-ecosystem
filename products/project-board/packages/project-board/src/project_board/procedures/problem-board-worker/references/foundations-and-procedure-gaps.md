---
id: project-board.skill-reference.foundations-and-procedure-gaps
title: Review Foundations And Close Procedure Gaps
summary: How to review a costly foundation decision (identity, storage, ordering, paging, durability, retention, data flow) and how to file and resolve a procedural lesson or a gap in this skill.
tags: [procedure, problem-board, worker, design-review, procedure-change]
keywords: [foundation, identity, authority, storage, canonical representation, ordering, paging, durability, retention, data flow, cardinality, concurrency, migration, procedural lesson, procedure revision, skill gap, owning rule]
see_also:
  - ./shared-runtime-state.md
  - ./collaboration.md
---

# Review Foundations And Close Procedure Gaps


For identity, authority, storage, canonical representation, ordering, paging,
durability, retention, and data-flow decisions, separate operator requirements,
existing contracts, assumptions, and new choices, and trace the real boundaries
and failure states: an existing assumption is not authority for a costly
foundation. Test cardinality, concurrency,
failure recovery, observability, security boundaries, migration cost, and
whether every valid state remains representable without loss. A count that
cannot be traversed, a cursorless truncated result, or a canonical row that
discards evidence needed later is a design failure even when the immediate UI or
test passes. Coordination does not waive this duty. Record consequential
alternatives and rejected assumptions in the journal, and where product policy
is undecided, give the operator the competing readings. For Redis or other
shared state, read [shared runtime state](shared-runtime-state.md).

A session that learns a procedural lesson files it against this package
before it settles the work it learned it in, as a revision or as an item naming
the rule and its evidence, and the settle summary names which. The capture
does not wait for the operator to ask.

When an operating failure shows that this skill could lead another agent to
repeat a mistake, resolve the gap: with clear evidence and a clear owning rule,
re-read the complete package, find every statement of the concept, rewrite the
owning rule so no vague, duplicate, or contradictory guidance remains, test the
contract, leave the revision to the merger, reinstall it, and tell active workers
the new revision: each loads it once, when `pb procedure verify` names it. With uncertain ownership or policy, create a work item with the
evidence and ask. A procedure update is a semantic revision of the affected
contract, never an append-only note, and it carries the rule with one clause of
reason. Incidents go to the journal, where search finds them when they are
needed, and not into this skill, which every session carries in its context.

