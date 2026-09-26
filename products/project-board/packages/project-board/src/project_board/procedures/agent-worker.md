---
id: app-ecosystem.project-board.procedure.agent-worker
title: Choose The Problem Board Worker Or Operator Procedure
summary: Routes machine setup and relay administration to operator runbooks and routes one selected Claude Code or Codex session to the versioned Problem Board worker skill package.
tags: [procedure, problem-board, setup, worker, coding-agent]
keywords: [worker skill, session identity, worker enrollment, relay operator, procedure package]
see_also:
  - ./first-time-setup.md
  - ./operator.md
  - ./live-acceptance.md
  - repo:app-ecosystem/products/project-board/docs/topology-and-flows.md
---

# Choose The Problem Board Worker Or Operator Procedure

Problem Board has two operational roles with separate authority and context.
Choose the role before loading detailed instructions.

| Role | Owned procedure |
| --- | --- |
| Configure a machine, target, filesystem roots, repository mappings, Connection Hub consent, or relay service | [First-time setup](first-time-setup.md) and [operator procedure](operator.md) |
| Enroll and operate one exact user-selected Claude Code or Codex session | Run `pb procedure show`, then read the installed `problem-board-worker` skill |
| Verify a live deployment and end-to-end delivery route | [Live acceptance](live-acceptance.md) |
| Maintain or test the client and its relay | [testing](testing.md) |

The user starts or resumes every coding-agent session. The operator configures
the receiving machine and grants authority. The selected session identifies
and enrolls itself, receives addressed input, sends visible replies, settles
leases, reports, journals, and detaches through the worker skill. Neither role
inherits Git, deployment, publication, or filesystem authority from these
procedures; repository instructions and explicit operator decisions own those
actions.

## Versioned Worker Package

The `project-board` distribution owns this installable source package:

```text
project_board/procedures/problem-board-worker/
  package.json
  SKILL.md
  references/
    identity-and-authorization.md
    delivery-and-recovery.md
```

`SKILL.md` is intentionally concise. It links to a focused reference only when
identity, authorization, delivery, or recovery requires that detail. Product
architecture remains in `docs/`; host administration remains in the operator
runbooks; dated evidence and decisions remain in project journals.

Every change to a file in the package ships under a new `revision` in
`package.json`, recorded with its source digest in the `project-board` source
package's revision ledger. Installed copies compare revisions, so an edit
under an unchanged revision reaches no session that already installed it. The
package's worker procedure contract test fails on such an edit and prints the
ledger line to add. The merger sets that revision at merge time, in merge
order; an author never bumps it, so on an author's head that test skips, and
the merger's run sets `PB_REQUIRE_REVISION_RECORDED=1` to make it fail
instead (`problem-board-worker/references/coordinator.md`, Merge).

Inspect source and installed state with:

```bash
pb procedure show
```

Install the complete package for future Codex and Claude Code sessions, then
verify every installed file:

```bash
pb procedure install --target codex --target claude-code
pb procedure verify
```

The installer stages one complete immutable generation containing the source
entrypoint, references, file digests, and package manifest. It verifies that
generation and only then atomically switches the installed `SKILL.md` loader
copy. An interrupted upgrade leaves the previous entrypoint and its references
readable. Old generations remain available to sessions that already loaded
their links.

Installing a new package does not change instructions already loaded into a
running model context. Tell an existing selected session to re-read its
installed `problem-board-worker/SKILL.md` after a verified upgrade.
