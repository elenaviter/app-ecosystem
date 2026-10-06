Part of [coordinator](../coordinator.md).

## Name who a wait is on, and receive before you repeat it

Every status that says work is waiting or blocked names four things:

- **Who acts.** The role `project operator`, unless the decision needs one
  named person (a credential only they hold, consent on their own account).
  Then name that person.
- **What exactly.** The decision or action, in words the actor can carry out:
  "approve raising the upload limit to 50 MB", not "approval".
- **The request.** The `message_ref` of the mail that asked for it, and the
  item it waits on ([collaboration Rule 11](../collaboration/rule-11-the-operator-is-asked-on-the-board-and-on-telegram-w.md)), so the actor
  and any teammate can open both.
- **Since when.** The UTC time the request was sent, from its record.

When the decision arrives, record who actually made it and when, from the
reply's sender and time, in the item's note or the project Facts.

**Receive before you declare or repeat an awaiting-operator blocker.** At the
next safe decision boundary (between two acts, never in the middle of a merge
or an activation), run `pb worker receive`, look for the reply correlated to
the request, settle it and act on it. Only then say what still waits. A queued
wake is not evidence of unanswered mail, and neither is your memory of having
asked: the answer can already be in your inbox. Why: a reply already in your
inbox ends the wait, and a wait without a named actor has no one to end it.

**Where the operator's answer arrives, and where yours goes.** A person's
Telegram **Reply** on a post returns to the agent that wrote the post as a
correlated `reply` in its board inbox, at its next inbox check
([Telegram](repo:app-ecosystem/products/project-board/docs/telegram.md)). An
agent's own `reply`, `update`, `progress` or `result` stays on the board and
does not reach Telegram. An acknowledgment the operator must see on their
phone goes as `decision`, `question` or `blocked`. Settling a message that
carries `operator_response` needs a correlated `reply` to the operator: a
delivered `decision` or `blocked` answer alone is refused with
`field_operator_response_required`. Send one substantive answer, and when
settlement asks for the `reply` as well, keep it to a pointer at that answer
rather than a second report.
