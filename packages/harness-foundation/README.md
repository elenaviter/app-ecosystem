# harness-foundation

The harness that lets a reactive agent work at scale.

An agent in production is not a loop on one machine. Conversations run on
an event bus, a turn can land on any worker, and everything inbound, a
prompt, a follow-up, an attachment, a stop, an external alert, is an
event addressed to a conversation, ordered and durable until consumed.
The harness is the machinery around the agent that makes this hold:
ordered delivery per conversation, a stop or a new thought reaching the
agent in the middle of its run, nothing lost, no turn running twice,
durable records of every turn, fresh workspaces, and trusted exits for
what the turn produced.

The same contract wraps agents that run their own loop, such as LangGraph
graphs or Claude Code: the existing agent comes in as is, with its own
loop and its own memory, it keeps its transcript, and the harness puts
events, workspace and records around it.

Scope boundary: the harness owns event delivery, turn lifecycle, records,
and workspaces. It contains no authority models (those are
[`prokura`](../prokura/README.md)) and no application behavior. It stands
on [`app-foundation`](../app-foundation/README.md).

**Version 0.0.1 claims the name.** The running implementation lives
inside [KDCube](https://github.com/kdcube/kdcube) today; extraction
follows the Prokura one.

Home: https://github.com/elenaviter/app-ecosystem
