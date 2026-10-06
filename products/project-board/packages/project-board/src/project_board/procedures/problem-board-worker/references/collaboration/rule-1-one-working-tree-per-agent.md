Part of [collaboration](../collaboration.md).

## Rule 1. One working tree per agent

Every agent works in its own working tree: a separate clone or a
`git worktree`, in its own workspace folder. No agent edits a tree another
agent also edits. The tree a runtime reads from is nobody's working tree.

Why: a branch does not separate files on disk. Two agents on two branches in
one directory still overwrite each other, and one still cannot commit without
carrying the other's half-finished work. This is the rule that removes the
first two failures above, on its own, before branches help at all.

Shape, the same on every host. The root is `~/.kdcube/pb/workspaces/<alias>`
(operator ruling, 2026-09-25: not in the user's home folder); a host set up
before the ruling keeps its old folders until a planned move:

```text
~/.kdcube/pb/workspaces/<alias>/   one agent, its own trees
    <repo>/                        home tree per repository
    kdcube-ai-app/
    app-ecosystem/
    <repo>@w<N>/                   a sibling tree for a second item in flight
```

On a machine that already holds shared checkouts (for example under `~/src`),
the workspace is made of `git worktree`s of those repositories, not clones:
objects and refs are shared, nothing is duplicated, and a pushed branch is
visible from every tree. The home tree per repository stays detached at
`origin/main` between items and takes `git switch -c work/<wN>-<slug>` for an
item. A second item in flight gets a sibling tree named for it, removed when
its change request merges.

```bash
# once per repository, from the shared checkout, as yourself
git -C ~/src/<repo> fetch origin
git -C ~/src/<repo> worktree add --detach ~/.kdcube/pb/workspaces/<alias>/<repo> origin/main
# a second item while the first waits on review
git -C ~/src/<repo> worktree add ~/.kdcube/pb/workspaces/<alias>/<repo>@w<N> -b work/w<N>-<slug> origin/main
# when its change request merges
git -C ~/src/<repo> worktree remove ~/.kdcube/pb/workspaces/<alias>/<repo>@w<N>
git -C ~/src/<repo> branch -D work/w<N>-<slug>
```

Rules of the shape:

- **A worktree is not a temp directory.** A tree under a session scratchpad
  or `/private/tmp` dies with the session or the reboot and leaves a
  `prunable` entry behind. On 2026-09-22 the two shared repositories on
  the shared host carried about seventy registered worktrees from four agents, a
  dozen already `prunable`, none under a workspace (round 1, finding eight).
- **Remove your own tree when its change request merges**, with the branch.
  `git worktree prune` is yours for your own entries. The coordinator prunes
  only entries whose directory is gone, which touches nobody's work. A tree
  that still exists is its author's to remove, asked first (Rule 2, branch
  ownership).
- **Nobody edits the shared checkout any more**, not even to land: a merge
  lands on the integration ref, and a runtime action loads the exact ref it
  releases, never a checkout ([coordinator](../coordinator.md), Reload, refresh,
  restart, step 1). Keeping a shared checkout current for reading is that
  machine's integrator's housekeeping, not a step before a runtime action. The landing
  steps in the worker skill remain for a machine that has no coordinator and
  no integrator yet.
- **A worktree cannot check out a branch another tree holds.** The shared
  checkout holds `main`, so a workspace tree is detached or on a work branch,
  which is what the rules want anyway.

The Problem Board client and relay run from one recorded source selection, not
from any working tree. `pb source use-release` selects an approved package
version and `pb source use-code` selects an exact App Ecosystem commit as one
release for both command and relay. A direct checkout invocation is a
visibly unpinned development process and never changes that host selection.
The integration ref is source history, not a runtime selection.

A worktree cut for a review or a fix has no installed dependencies. Borrowing
the live checkout's `node_modules` by symlink works for third-party packages
and silently fails for the repository's own workspace packages: the
`@kdcube/*` links inside that `node_modules` point back into the live
checkout, so a worktree's `components-react` typechecks against the live
`components-core`, not the one on the branch. Seen on 2026-09-22 reviewing
KDCube #258, where new exports "did not exist". Make `node_modules` a
directory of links in the worktree and point the `@kdcube/*` entries at the
worktree's own `packages/`, then build the dependency package before the
dependent one. Python has no such trap: `PYTHONPATH` names the tree.
