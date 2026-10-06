Part of [collaboration](../collaboration.md).

## Rule 11. The operator is asked on the board, and on Telegram when it is urgent

**Anything that waits on the operator is a work item assigned to them.**
Whoever needs the operator, worker or coordinator alike, for a test, a
decision, an approval, a choice or an acceptance, puts it on a work item
whose assignee is the operator, so it appears in the operator's own lists:

- a finished change that needs their check: `pb worker report --reviewer
  operator` with `--merged` and `--deploy` (Rule 6), or `review.assign` with
  `operator`;
- a decision or a choice on work that is not in Review: set the item's
  assignee to the operator (`work.assignee.set`), or file a small decision
  item assigned to them (Rule 14).

The item says exactly what to do and what to expect: the steps, where, the
result that means it works, and how to report the result with the controls
the person has (below). Then send a board message to `operator`
naming the item and the action, as `decision` or `question` so it also
reaches their Telegram, saying "urgent" when it is. A question the message
carries states:

- the question
- the options, each with what it costs
- your recommendation
- what you do while you wait

**The item itself carries what the operator needs.** The operator works from
the board, their inbox and Telegram, not from an agent's terminal, and the
board shows them the item's fields: its Result, "How to check this"
(`review.look_at`) and "What could not be verified"
(`review.could_not_verify`). Routing (`review.assign`, `work.assignee.set`)
changes who acts next; it does not rewrite those fields. A checklist an author
wrote for an agent reviewer stays on the item and tells the operator
the wrong thing. So whoever routes an item to the operator rewrites
them for the operator with `plan.item.update`, in the same step:

- `result`: what is true now, in plain words: what was merged (exact
  commit), where it runs, and what proved it, with the evidence ref;
- `review.look_at`: the operator's steps: what changed, in one sentence;
  what to look at or try, and where (board, inbox, a page); the result that
  means it works; and the decision, in the controls below;
- `review.could_not_verify`: what is not proven and not claimed.

**The person decides with Notes, Status and Assignee, and nothing else.**
The item dialog a person sees has those controls and the fields above; it has
no Accept, Return or verdict button, and none is added for this. So the
decision is always written as:

- write what you found, and your decision, in **Notes**;
- if it is fine, set **Status** to **Done** and save;
- if it is not, set **Status** to **Working**, choose the agent in
  **Assignee**, and say in Notes what differs.

Never name a control the dialog does not have. Why: the operator, 2026-10-04,
"there's no such thing as "Accept"? there's the status change and assignment
... you keep recommend me what i do not have and do not want to have", and
"i want to be able to change the status and put assignee. i do not want any
"accept" and other 100 buttons please".

Write them so the operator can act from the item alone, with no other
message. The `decision` or `question` message names the item and points
to these fields.

**An operator's question is answered where the operator works.** When the
operator asks you about an item, in any channel, your terminal included,
the answer goes on the item (its fields, or a note when it is history) and in a
board message to `operator`. An answer only in your terminal is not durable.
When the question shows the item was unclear, correct the item: answering the
question does not fix the item. Then tell them where the answer is.

Their reply arrives as a correlated message you answer like any other. After
routing, read the item back (`project.plan.item`). Check that the operator is
its assignee and that its Result, "How to check this" and "What could not
be verified" are the ones you wrote for them: an action that is not in the
operator's lists, or that they cannot follow from the item, does not exist
for them. Never leave an operator action only in an agent's
terminal or only in mail between agents (operator, 2026-10-03: "if something
waits for me i expect to see it in my assignments"). When the routing or the
message is refused, never work around the permission: report the refused
action, its code and who can clear it (the coordinator, or the operator for a
Card) on the item and to the coordinator. Do not send the same unchanged
request again; a reminder names what changed or the deadline that passed.

A Claude Code worker session runs without an interactive prompt tool. It is
started with `--disallowedTools AskUserQuestion` (first-run reference), so the
board mail is the one way a question reaches the operator.

Why: the operator is not watching every worker's terminal. A dialog that
waits in one session holds that worker still, and the operator learns about it only if
they happen to look at that screen. On 2026-09-23 a coordinator asked the operator an
approval question in a terminal dialog. The operator's ruling: "in PB the agents cannot
be sure the operator is looking into their terminals. and if there are inputs
needed, the agent must send this in project chat to operator, or if urgent
then also in telegram." On 2026-10-03 an item was routed to the operator
with the author's source-review checklist still shown as "How to check this".
The operator asked about it in a terminal and the answer stayed there. The
operator's ruling: "this "here" is not a durable place. i expected you to edit
the work item and put the nornal steps for operator (me) to follow to check"
and "the tickets assigned to operator must contain the information for
operator."
