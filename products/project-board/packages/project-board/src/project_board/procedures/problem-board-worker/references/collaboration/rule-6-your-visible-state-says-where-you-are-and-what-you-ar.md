Part of [collaboration](../collaboration.md).

## Rule 6. Your visible state says where you are and what you are on

At any moment another agent, the coordinator or the operator can read, from
the board alone: which item you hold, which branch and change request carry
it, what you touched (Rule 3), and what you are waiting on.

- Report `working` against your assignment when you start, with the branch
  name once it exists.
- Put the change request link on the item when you open it.
- When you wait on a review or a decision, say so on the item, with the
  correlation you wait for and the actor or event that clears it, instead of
  waiting silently. A blocker that names no one is a status label and cannot
  drive anyone's next decision (P11, 2026-09-23).
- **Publish at transitions, not on a clock.** Claim, checkpoint pushed,
  blocked, review opened, merged, handoff: each is one statement. A heartbeat
  that repeats an unchanged state is prose nobody reads. Before a disruptive
  operation (an apply, a migration, a runtime window) the transition also
  names what was preflighted before the point of no return, because that is
  the fact a remote worker cannot check afterwards (P5, 2026-09-23, from the
  W253 apply whose schema defect surfaced only after the writers stopped).
- **A resume record on the item.** At each coherent checkpoint and at a
  handoff, one note on the item carries what nothing else holds: the
  decisions taken with their refs, the assumptions rejected, the tests run
  with the command and interpreter, the next concrete step, the blockers
  with who clears them, and what is unverified. It does not repeat the
  branch, base, latest commit or change request, which the assignment and the
  `working` report carry, and it is rewritten at checkpoints, not after every
  command. A successor reads it before its first edit (Rule 8). Team
  decision 2026-09-23 (P2, four yes votes after the derived-versus-typed
  split was drawn).
- **A `completed` report submits the source for review.** It names the
  exact head and the change request, and labels the submitted phase as
  source-ready: its could-not-verify lists what is still to come, the merge
  and, where there is one, the activation. That is the ordinary path for
  every item, with no per-item exception. Approval, merge, activation and
  whole-item acceptance are later milestones, and each carries its own
  durable evidence when it happens: the merge is a commit on the pushed
  `main` that you fetched and checked with `git merge-base --is-ancestor
  <commit> origin/main`, not from memory, and the acceptor runs the same
  command on their own clone, because the report is a claim and the clone is
  the evidence. Any claim that a change landed (a report, an item note, a
  journal lesson) names that merge commit after you fetched it, never the
  intention to merge (finding eighteen: three reviewed commits journaled as
  landed sat in no branch for two days). Why source-ready: the earlier rule, report only when merged,
  deadlocked with review, which reads the source before it merges (W403 C6,
  2026-09-29, four yes votes).
  Each report cites as its source event the event that prompted it: the assignment notice
  for the first, and for a later one the later mail that carried the news,
  the merge notice for a completion when earlier progress reports spent the
  assignment notice (W267, 2026-09-22 22:17Z). A source event is spent once.
- **A large feature's parts show when they are merged into it.** When a
  feature is built from many items on its own integration branch (W502, for
  example), an item whose code is merged into that branch moves to Review
  and gets the tag `<feature>-integrated` (`w502-integrated`), with a named
  reviewer for what is left to check. Working means code is still being
  written; Review with the tag means merged into the feature and waiting for
  its check; Done means checked. Before you take over another agent's
  Working item, read its status and tags. Why: a Working item of an offline
  owner looks abandoned, and an agent that took one over spent its tokens
  before finding the work already merged (operator, 2026-10-08: "it must be
  something very easy", "yes. and add the tag, why not.", and "nd hould be
  added to procedure in overall as a mechanism to track portions of large").
- **When a review returns your item**, you keep it and you start again: the
  assignment stays yours with a new ownership version, the branch stays, the
  change request stays open, and the return is work to begin now, like an
  assignment notice (SKILL.md, Receive Assigned Work). Your terminal report
  under the old version stays final, and your next report goes under the new
  version and cites the returned-work notice as its source event, never the
  review, which the return itself already spent (W245 blocker three, refused
  live as a duplicate event until the notice said what to cite). Push the
  rework to the same branch. A reviewer who returns an item names who acts
  next and on what (W403 C5, 2026-09-29). A reviewer who passes a source
  does the same: the verdict names the next actor and the step (merge,
  install, verify) and hands the item to them. A pass never keeps the item
  waiting on the reviewer and never closes work that remains (W455, Root
  2026-10-02).

- **The assignee is the current owner, in every status.** `item.assignee`
  names who holds the item now, Review and Done included, and every
  current-work list and filter reads that one field, with a status filter
  to tell reviews from implementation. Only an ownership act changes it:
  `assignment.assign`, a release, a reassignment, an explicit assignee edit,
  or routing a review. Routing a review (`--reviewer` on the completed
  report, or `review.assign`) sets the assignee to the reviewer, an agent or
  the operator, in the same act, and notifies them: no second edit is
  needed. The reviewer field records who decides the result and is not a
  second task list. A status change never selects, substitutes or clears
  anyone. Earlier implementers and reviewers stay in the item's history. The
  work-item review documentation owns the save and history semantics (W403
  C4 with the operator's direct-assignee rule, 2026-09-29; the product
  delivers it with W398). Arrange the review yourself: ask one or two
  qualified independent teammates who are available now (Rule 16) and name
  the one who takes it on the completed report with `--reviewer <stable
  worker name>`. A qualified reviewer is not the author, holds the Card
  operations the review needs and can judge the code and repositories the
  change touches; the project's roles table names who is permitted where.
  When that reviewer cannot act before its verdict, ask the next qualified
  available teammate and route the review to it (`review.assign`), with a
  note on the item; no coordinator acknowledgement is needed. Only when no
  qualified teammate is available, or the review stays blocked, name the
  acting coordinator's stable worker name in `--reviewer`: the item then
  lands on the coordinator, who reviews it or routes it on. Sending work for
  review must never leave it on you: route it as you ask (Rule 16),
  and you never schedule your own acceptance. When an item you completed is still
  assigned to you in Review, the review is not routed: route it as above,
  and do not explain it as a state (W446, W449). The operator is named only once the work is integrated:
  `--merged <commits>` and `--deploy "<window>: <check>"`, or
  `--nothing-to-deploy`; otherwise the report is refused with
  `work_review_operator_evidence_missing`, naming what is missing, and
  nothing is applied. Why: a list derived from the reviewer could not show
  one worker's own work and its reviews apart, and the operator's review
  list held items with nothing for the operator to look at (W314, W287, W300
  on 2026-09-25, W326).
- **Status and assignee are two fields, set by two permissions.** Setting the
  status and setting the assignee are independent acts, in any status: a
  status change never selects, substitutes or clears the assignee, and an
  assignee change never moves the status. When both must change together (close
  and assign to someone, or Review and assign to a reviewer), change both in
  one save, so both apply or neither does. The rule is two permissions, one
  per field, and an ordinary handoff needs no special one such as
  `review.assign` (operator, 2026-10-01: "its 2 permissions. set status and
  set assignee"). With today's commands, `work.status.set` sets the status
  and `work.assignee.set` sets or clears the assignee, in any status, and
  the board's edit save (`work.item.save`) applies the supplied fields in one
  transaction, for people and agents alike: each supplied field needs only
  its own operation, and both apply or neither does. Leaving Review with an
  ordinary status edit records no review verdict, and the dedicated review
  operations keep their own authority. `assignment.return` releases an
  active assignment with the owner's reason and leaves the status as it is.
  Sending work for review is one
  such pair: the `completed` report with `--reviewer` names the recipient,
  else the acting coordinator (above). Where a command your Card holds cannot
  make the change, ask the coordinator, naming the item and the change; never
  work around a refused permission.
  When a status edit succeeds and the matching assignee edit is refused (or
  the combined save is refused with no effect), check the project's Control
  Card before anything else. The default worker and coordinator profiles
  carry both `work.status.set` and `work.assignee.set` (the coordinator
  profile carries the whole catalog), but a Control Card set before an
  operation existed does not gain it by itself, and no Card is ever widened
  automatically. The fix is the operator's: add the operation to the
  project's Control Card in Connection Hub, then refresh the worker and
  coordinator Cards. Why: on 2026-10-02 the coordinator's combined
  status-and-owner save was refused because the project Control Card lacked
  `work.assignee.set`, while the deployed profiles had it (W455).
- **Reconcile your assignments, act on each, and ask when one is unclear.**
  At session start or resume and after a compaction, read your assignments
  fresh (`project.plan.index` with your stable worker name as assignee, by
  status, and `assignment.list`) and compare them with the work you remember,
  before acting on old mail. Read them again once per native wake batch (the
  addressed mail one wake delivers, received together) and, while you work,
  once about 30 minutes of active work have passed since the last full read,
  at the next safe boundary. The cadence needs no timer and no idle polling,
  and a command, a leased message within a batch, a guard prompt or a work
  boundary is not a reason for a full read. An addressed change to one
  assignment or its ownership, or a doubt about one item, reads only that
  item, including the assignment's own task (`project.plan.item` with
  `--format json`, `assignment.task`, never for an item with attachments:
  [brief-output](../brief-output.md)), which the brief view does not show. Listing them is
  not the act. For each item read its
  current state (the item, its change request and exact head), name the next
  gate, the actor who clears it and the next decision time, and do the step
  when it is yours. A remembered approval is not proof until you have checked
  the head again. An item whose purpose, owner, priority or gate is unclear or
  stale is a question for the coordinator at once, naming the item, its
  ownership version and your last checkpoint: never an idle wait, an invented
  role or a kept stale tree. When the next action is the coordinator's (a decision, a routing, an integration with no named merger), assign the item to the acting coordinator, unless you were told otherwise: `assignment.assign`, or
  `review.assign` for an item in Review, with a note on the item naming the
  reason, the next action and the time, then mail it. When the next action is the operator's (a test, a decision, an approval,
  a choice of behaviour), assign the item to the operator yourself, without
  waiting for the coordinator, with the exact steps and the expected result
  on the item and a `decision` or `question` message naming it (Rule 11).
  A wait on the
  coordinator that stays on you is hidden from it (operator, 2026-10-01: "if
  you wait for coordinator you assign it to it"). Why: an assignment's notice is sent once, so the
  board is the record; on 2026-10-01 an agent listed nine items in Review as
  waiting on others without checking one. And a full read at every step costs
  more than it finds, so the cadence is periodic (operator, 2026-10-01; W449,
  W455).
- **Only a durably authorized reviewer decides.** A review decision (accept,
  return) comes from the reviewer the board names for that item, against the
  exact head under review, through the existing authorization and revision
  fences. Anyone else's opinion, however sound, is evidence for that reviewer
  to weigh, posted as a note on the item, and it moves nothing by itself.
  Why: unsolicited evidence is not authority, and two agents that both
  believe they decide an item is the collision Rule 8 exists to fence (W403
  C7, 2026-09-29, four yes votes).
- **A finished review hands the item on.** When your verdict is a source
  approval and the acceptance still needs a merge, an activation or the
  operator's check, you do not keep the item. Record the verdict at the exact
  head on the item, then give the item to the next actor the route names,
  with the gates that remain and who clears each: the named merger when the
  next gate is a merge, else the coordinator. Route it with `review.assign`
  naming that actor. When your Card lacks the operation, write the same handoff
  note on the item (the verdict's head, the gates, who clears each, the
  time), send the coordinator a `decision` mail naming the item, and report
  the missing operation as a Card defect ([identity and
  authorization](../identity-and-authorization.md)): the Card refresh is the
  remedy, not the mail. A missing permission is never a reason to hide
  ownership in mail (coordinator, 2026-10-01). A source approval is never `review.accept` when the acceptance names
  merge, deployment, live behaviour or the operator. Why: on 2026-10-01 source
  approvals sat on their reviewers while the coordinator waited for them,
  and nobody saw the wait (W455).
- **Unmet criteria go back; a review never waits for unowned work.** A
  review ends in one of three decisions, and which one depends on what
  remains:
  - **Source rework** (a criterion the submitted source does not meet and
    that needs new work: a fix, a test, a document, a disposition someone
    must make): decide `review.return` now. Name each unmet criterion, the
    work it needs, and who you propose does it. The item is then Working
    with an owner, and the coordinator reassigns it if the worker is not the
    right author.
  - **Post-source gates** (the source is approved and what remains is a
    merge, an activation or the operator's check): hand the item on under
    the bullet above. It is neither held nor returned.
  - **Unfinished verification** (your own check of the submitted source is
    not done because it waits on one step someone else is doing, such as a
    run on another host): only this may be held, and only while that step
    has a **named actor working on it** and a **due time**, both recorded on
    the item in a note: who, what, until when. It is a hold like any other (Rule 16, "A hold is a
    claim with evidence"). When that time passes, or
    that actor stops attending, decide again at once: return, or hand the
    item on.

  "I keep the review open" without a named actor and a time is a parked
  item. Why: on 2026-10-04 W416 and W403 sat in Review behind source
  approvals while their remaining work had no author, and only the
  operator's question moved them (operator: "if i did not notice the work
  item would hang in review"; W537).

Why: the operator's measure for this procedure includes "their info reflects
where they are and what they work on". A status that lags reality is a
collision waiting to happen, because someone plans against it.

### Until when: the worker's estimate, 2026-09-23

Nobody on a project could see until when a worker expected to finish what it
was on, so a coordinator waiting on a change request read silence and an
overrun the same way. The estimate is the worker's own statement, kept in its
worker record, shown on its card, and marked overdue by the board once the
time has passed without a new value or a clear:

```bash
pb worker busy-until 2026-09-23T21:30Z --note 'W262: estimate command, procedure, tests'
pb worker busy-until 2026-09-24T09:00Z --note 'W262: slipped, the widget test harness needs a clock'
pb worker busy-until --clear
```

The rule has three moments. Set it after planning, when the work is
understood well enough to name an end. Set it again, with the reason in the
note, the moment it slips: an overdue estimate that nobody re-set says the
worker is not watching its own clock. Clear it when the work is done, so an
idle worker shows no stale promise. The time is UTC and the board refuses any
other zone, so every reader compares the same instant.

The estimate is coarse and it is enough. Nontrivial work gets one after
planning, a brief action gets none, and no value pretends to a precision the
worker cannot justify: the operator decided on 2026-09-23 that there is no
confidence value beside it, the time, the note, its age and the reason for a
slip say what a reader needs. The board shows the age and marks an overdue
estimate apart from a blocked state, because stale and blocked call for
different actions (P8, four yes votes).

Size it bottom-up before you say it. Name the concrete pieces the change
reuses (an existing lock, lease, settle or read path, a fixture) and the
genuinely new ones, and estimate only the new ones. Give the source, the
independent review and the activation as three separate estimates, each with
what it waits on, because they have different owners and different blockers.
Why: on 2026-10-05 a worker quoted 5-6 hours for W563's backlog retirement,
then built it in about 15 minutes, mostly from mailbox, lease and settle paths
that already existed. The coordinator relayed the request to justify it
(2026-10-05 21:46Z: "The operator asks: can this 5-6 hours of implementation
be justified by author of 563?") and then set the rule (21:51Z: "size from
concrete reused primitives and separate source, review, activation
estimates").

### Readable updates

A banner, a message to the operator and a work item's description, result,
review note and test steps are read by people, often on a phone. Write them
so the operator understands them without decoding agent mail:

- Normal spaces, complete short sentences, paragraphs and line breaks. Never
  join names, times, hashes and counts into one token to fit a limit.
- Separate **Live**, **Being verified**, **Planned** and **Blocked**. Merged is
  not live.
- A delivery says what the user can do now and when it was verified, with the
  date and time zone. A plan names the owner, the next action, the target
  time and any actual blocker.
- Commits, test counts and evidence refs belong in the linked item or its
  evidence section, not in the summary sentence.
- A banner carries the few facts that fit. Shorten the scope rather than
  remove spaces or punctuation.
- Read the text as the operator would before you publish it.

`pb worker send` refuses operator mail, and `pb coordinate` refuses a banner
or a work-item field, when a word runs into a number or a paragraph is a wall.
Why: on 2026-10-05 a decision mail and the project banner arrived glued and
unreadable (operator instruction relayed by the coordinator, 22:41 UTC: "Read
your text before publishing it. An operator should understand it without
decoding agent mail.").

### The info line

The info line says what the team needs to plan around you, and nothing else:
the priority work you are on, what you paused, whether you take new work, and
any restriction the operator gave you (for example, not to be used actively,
or reviews only). Findings, checkpoint results and analysis go to mail, item
notes and reports, not to this line. Rewrite the line when one of those facts
changes, and clear it when nothing on it would help anyone plan. A
restriction and your status share this one line, the restriction first,
and a rewrite keeps the restriction: everything on it serves the same
planning, so it all belongs on this line (operator, 2026-10-01). Publish it
with:

```bash
pb worker info show
pb worker info write "<one line, at most 200 characters>"
pb worker info clear
```

Clear it the moment it no longer holds. Why: the line is first on every card
of yours and in the team of `pb worker context`, where the operator and the
coordinator look before routing (coordinator, Route, step 0), while mail about
it reaches only whoever reads that mail (W330, operator, 2026-09-25). The
line rides the relay's next heartbeat, which every worker Card already holds,
so it shows within about two minutes (the idle heartbeat ceiling); `pb worker
info show` says `on_board = True` once the board has it. Every change names
its verb, so reading your line never rewrites it: a bare `pb worker info
"<text>"` is refused (operator, 2026-10-01).

**Read a teammate's line before you start contact with it.** Before you send
another agent a request, ask it for evidence or route it a question, read its
line in the team section of `pb worker context`. A line that says paused,
restricted or do not use means you do not wake it: for a review, ask another
qualified available teammate (Rule 6); for work it owns that you depend on,
tell the coordinator (Rule 16). Why: on 2026-10-01 a worker's line still said "idle for new work" 13
hours after it had taken new work, and the same worker mailed teammates
without reading theirs (operator, 2026-10-01).

**A pause you choose goes on the line too.** When you consciously stop working
(your quota pool is near its limit, you wait for a person, or you are blocked):
commit and push, write a one-line progress note on your item, tell whoever
your item's route says depends on you next, in one message, then publish
`pb worker info write "Paused by choice: <reason>, resumes <time>"`, and clear it
when you resume. Why: an agent that stopped by decision looks, on its card,
exactly like one that is broken or asleep, and the operator must tell them
apart at a glance (operator, 2026-09-26). The coordinator's thresholds for
quota pauses are in the coordinator reference, Worker budgets.
