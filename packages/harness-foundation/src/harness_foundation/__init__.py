"""harness-foundation: the harness that lets a reactive agent work at scale.

Ordered per-conversation event delivery, mid-run steering and stop,
durable records and workspaces, and one contract for native agents and
agents that run their own loop (such as LangGraph or Claude Code): the
existing agent comes in as is and the harness puts events, workspace and
records around it. No authority models (those are `prokura`), no
application behavior; it stands on `app-foundation`.

Version 0.0.1 claims the name; the running implementation lives inside
KDCube today. Status: https://github.com/elenaviter/app-ecosystem
"""

__version__ = "0.0.1"
