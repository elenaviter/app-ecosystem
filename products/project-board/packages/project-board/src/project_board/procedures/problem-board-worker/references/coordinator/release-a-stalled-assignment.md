Part of [coordinator](../coordinator.md).

## Release a stalled assignment

1. `assignment.return` (Release assignment) takes the item `work_ref` from
   `project.plan.item`, its `expected_revision`, a reason, and an
   `idempotency_key` you generate. It needs an active assignment.
2. Release changes ownership only: the assignee is cleared, the released
   worker becomes the preferred reworker, and the ownership version advances,
   which refuses any later report from that worker. Status stays as it was.
   Why: assignment and status are separate facts, so neither assigning nor
   releasing says anything about progress.
3. To also change the status, make that a separate status edit, by a caller
   permitted to set status.
4. Tell the released worker, with the reason, in a correlated message. Then
   route the item again or leave it for the plan.
