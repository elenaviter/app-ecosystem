Part of [coordinator](../coordinator.md).

## Set a teammate up to work

The team prepares the setup a teammate needs. When a worker reports a missing
development environment, access grant, repository, or piece of project
context, the coordinator arranges one durable path forward:

- update the owning procedure for setup shared by projects;
- update the project's facts or environment page for project-specific setup;
- ask the teammate who already knows the answer to prepare and record it.

The worker builds from that prepared path and reports each additional gap. The
coordinator turns every reported gap into a procedure or project-page fix, so
the prepared path is ready for the next teammate. When a new agent joins, the
coordinator welcomes it, names the project's prepared context, and asks it to
report setup gaps as it finds them.

**Rehearsing an official flow gives no hints.** Guiding an agent step by step
is only for prototyping a flow whose behaviour is not yet known. When an
official flow is rehearsed (onboarding, install, hand-over), the coordinator
gives no hints: the agent uses only the installed client and skill. Each place
it has to guess is a gap, fixed in the skill or the client, reinstalled, and
then the agent re-reads the skill and continues from it, reporting anything
still unclear. Why: on 2026-09-26 the coordinator told a new agent its
workspace path directly, it cloned into it, and the flow the operator wanted
tested was never tested (operator, 2026-09-26).

**A returning worker starts from the current procedure** (operator,
2026-09-29). A project authorized to develop Problem Board (a maintainer
project, as its facts page states) changes reviewed procedure and client
source while a worker is idle, and an installed skill copy stays at the
revision it was installed with. When a worker is resumed, or returns after a
long idle period, before it gets substantive work the coordinator:

1. Compares the host's selected source (`pb source status`) and installed
   procedure revision (`pb procedure verify`) with the current reviewed or
   published revision.
2. Starts the update or reinstall the host's policy allows (runtime actions,
   Client Source Selection, and `pb procedure install`).
3. Asks the worker to load the complete current skill when its loaded
   revision differs from the installed one, and the worker confirms the
   revision before it begins.

A worker whose info line says paused, restricted or do not use is left
asleep until it is legitimately resumed: waking it only to update spends its
budget for nothing. A project that consumes Problem Board follows its
published-release policy for the same check.
