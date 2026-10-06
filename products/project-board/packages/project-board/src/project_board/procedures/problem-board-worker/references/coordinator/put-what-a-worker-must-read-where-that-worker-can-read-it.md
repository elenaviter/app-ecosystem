Part of [coordinator](../coordinator.md).

## Put what a worker must read where that worker can read it

A route that points at something the assigned worker cannot open is not a
route. Before sending a pointer, ask what that worker's Card can read and what
its machine can reach.

- **Text the worker needs in order to do the work belongs in the item**, in its
  description or in the assignment's task instructions. Those travel with the
  assignment, survive an unread inbox, and every worker Card can read them
  through `project.plan.item`.
- **The current instruction lives in the item's description or task; notes
  are the history.** Worker Cards can read notes (`plan.notes.list`), but a
  worker reads the description and task first, and a note can be one of
  sixty. When an instruction changes, rewrite the description or task so it
  states what holds now, and say there which earlier briefing it supersedes;
  the note records the change and why. Never leave the current instruction
  only in a note or a mail.
- **Never send a local filesystem path as the carrier.** A path is bound to one
  machine and one user. The moment a worker runs on another host it points at
  nothing, and the failure looks like a worker ignoring instructions.
- **Files use the Board lane, not a shared filesystem.** Use `worker send
  --attach` for any worker or operator mailbox. To pass on an addressed
  message, use the exact lease-bound `worker forward` described in
  [delivery and recovery](../delivery-and-recovery.md); do not copy a sender's
  local path. The normal recipient rules still apply.
- **A repository ref is portable, a working-tree path is not.** Point at a
  committed file by repository alias and path, never at `/home/...` or
  `<home>/...`.

A refusal a worker reports while following a route is the coordinator's defect
first: fix how the work was handed over, and raise the grant when the refusal
was the right rule applied to the wrong case.
