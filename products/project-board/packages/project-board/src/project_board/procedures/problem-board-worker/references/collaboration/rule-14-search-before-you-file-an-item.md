Part of [collaboration](../collaboration.md).

## Rule 14. Search before you file an item

A worker's Card may create plan items (`plan.item.create`). The board
numbers each new item as the project's next free W; send no `item_key`
(W551). Before filing one, search the plan (`project.plan.search` with the subject's words) and
read what comes back. When a related item exists, update it or add a note or
an acceptance line to it instead of filing a duplicate. A small change or
lesson is never its own item: it goes onto an open item. A new item is for a
distinct deliverable with its own review. Routing finds new items in the
plan; mail the coordinator about one only when it needs a routing decision
now (Rule 16, point 5). Why: the operator allowed
workers to file items on these two conditions (2026-09-26 21:1xZ), and asked
for no items for very small things (23:38Z).

Filing an item means filing its description and at least one acceptance
line in the same pass: what the work is and why, and when it is done. A
title alone is not a filed item, and the board refuses it with
`work_item_field_required`, naming the missing field (W244). Write the
keywords too, so search finds it. Why: the operator, 2026-09-20, on items
filed with a title only: "why we have entries with no description? and no
keywords? this is unacceptable. what and where tells such work item ever
can be created? it is wrong".
