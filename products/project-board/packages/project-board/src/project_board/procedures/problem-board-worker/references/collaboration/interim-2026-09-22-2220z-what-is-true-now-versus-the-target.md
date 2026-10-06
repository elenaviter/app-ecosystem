Part of [collaboration](../collaboration.md).

## Interim, 2026-09-22 22:20Z: what is true now versus the target

Historical record of that night, kept for its findings. Where it names who
merges, installs or verifies, Rules 2, 5 and 16 decide today.

| target | shared host now | second host now |
| --- | --- | --- |
| one working tree per agent | each agent in its workspace; an agent still in a shared checkout makes its own | one workspace per agent, own clones |
| branches and change requests | every item since 20:10Z, twelve change requests merged or open | from the first task |
| pb runs from a pinned copy | selected package version or composite release, reported by `pb source status`; the shared checkout is not a runtime | selected package version or composite release, reported by `pb source status` |
| coordinator merges and pushes | the coordinator, on the host | the coordinator, remote |
| review record | board mail plus a pinned change request comment, one GitHub account for all | same |

Nobody lands by hand any more on the shared host. On a machine with neither a
coordinator nor an elected integrator, landing an approved change into a
shared checkout goes like this, and the skill points here: take a bounded
turn for a shared Git operation, announced on the dashboard, copy to
`.landing` names in one pass, move in one pass, verify with `cmp`, run both
suites live. Prepare and verify in a private index (`GIT_INDEX_FILE`), commit
by explicit path, then refresh the shared index with
`env -u GIT_INDEX_FILE git read-tree HEAD`, or the next ordinary commit by
anyone records a reversal. Never `git add -A`, never `git stash`.
