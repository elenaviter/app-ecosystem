Part of [collaboration](../collaboration.md).

## Rule 9. What is published is safe to publish

Everything a worker publishes for another reader (a status, a resume record,
a handoff, observed paths, a WIP push) carries stable refs, hashes and
redacted evidence, and never a secret, a bearer credential, the name of an
untracked file, machine-private data, or the name of a person or organization
we work with. A WIP push to a public repository (kdcube-ai-app,
app-ecosystem) gets the same check as a final push, and observed files in
flight are tracked paths only. Why: visibility that leaks is worse than none,
and the checks that exist for a final change request (Rule 2) were never
meant to be skipped by pushing earlier (P13, 2026-09-23, four yes votes).
