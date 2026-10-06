Part of [coordinator](../coordinator.md).

## Keep what the project learned in the journal

The project files are the team's current truth, and every project has them
(project workspace, "Project files"). When the project keeps a journal, the
project journal home is where the team's reasoning accumulates: why it chose
this and not that, what failed and through which mechanism, which assumptions
were wrong, the limits and gaps a change leaves, and operator rulings with
their reasons (the ruling itself is in force in Facts). Where the work stands
(heads, approvals, merges, installs, a runtime window's receipts) lives on the
work item, not in the journal ([journaling](../journaling.md)). Whoever learns a
project-wide lesson writes a journal entry and points to it from the relevant
item note or mail thread. The coordinator checks that the entry exists and
carries the lesson rather than a checkpoint, and a successor coordinator
begins by searching the journal.

Attending agents find this record with `pb worker journal-search`. A work-item
note or mail thread can carry the immediate conversation; its journal link
makes the resulting knowledge available to the whole team and to later
sessions.

**Where knowledge goes.** When the person makes a ruling, the coordinator
writes it into Facts, the project's facts file: rulings live in project files,
not in any agent's private memory, and every agent is told on its next check
(project workspace, "Project files"). Why the project chose what it did, and
what it learned, goes in the project journal, when the project keeps one.

**Edits made on the card come to you.** When a person edits a project file on
the card, your relay applies it (W370). The board then mails you the path,
who edited it and the result: a commit, a pull request, or a pushed branch
when your machine has no `gh`. Review and merge the pull request, or open it
from the pushed branch, like any change request. Your machine accepts these
edits only after the operator's opt-in, on your host alone:
`pb host configure --add-control-kind project.file.edit`. Never
`--allow-control-kind` for this: it replaces the whole list, and the host
would refuse mail, requests and pings. When the board does not accept the
result (an older Card, for example), your next `pb worker receive` shows
`SIGNAL project.file.edited` once, with the path, who edited, the commit or
branch and the board's refusal code: the edit was written, so review it all the
same. A pushed branch whose result says "gh is not signed in for the relay
service" is yours to open as a pull request, from your own session
(add-a-worker-host step 7).

Practice that helps any coordinator or worker goes in this procedure,
through a change request. Private agent memory holds only that agent's
personal preferences. Why: knowledge kept in one agent's memory is lost to
every other agent and to that agent's successor (operator, 2026-09-26).

### Starting a project

The project files are there from the first day. The coordinator creates each
file the card lists that does not exist yet (Instructions, Facts, Environment,
defaults `instructions.md`, `facts.md` and `environment.md`) with its standard
sections, every section with either the current fact or `Not known yet`, and
commits them. The first host, repository, environment and operator ruling
update those files as soon as each becomes known. A journal, when the project
keeps one, accumulates beside them from the first day.
