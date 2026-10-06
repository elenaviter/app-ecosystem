Part of [collaboration](../collaboration.md).

## Rule 13. A behaviour change carries its documentation in the same change

Every change to Problem Board behaviour updates the public documentation
(`repo:app-ecosystem/products/project-board/docs/`) in the same change; when
the code lives in a private repository, the documentation change is a paired
change request in app-ecosystem, named in the first one and merged with it.
The reviewer checks it and refuses a behaviour change without it. Why: the
concepts an agent needs to explain Problem Board had been kept only in private
pages, so an agent with only the public client could not answer who edits
which Card (operator, 2026-09-26).

**Where a rule lives.** A rule any coordinator or worker needs on any project
goes in this common procedure, reachable from the skill. A project's names,
source bindings, priorities, batch roles and actual route tables go in that
project's files. A reusable runtime command of one product goes in that
product's guide, linked from here or from the project files. A rule written
here names no project's hosts, tickets or incidents: the incident that
motivated it goes to the project's journal. Contributing to a shared
repository from another project's board is ordinary work under the actual
scope, permissions and review: a project's primary product is its focus, not
exclusive ownership of a repository (operator, 2026-10-03).
