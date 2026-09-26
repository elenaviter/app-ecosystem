---
id: project-board-projects-runtimes-and-refs
title: Projects, Runtimes And Refs
summary: The three kinds of Problem Board project and what each declares - its instructions, repositories and runtimes - so one worker procedure serves them all.
tags:
  - project-board
  - projects
  - runtimes
keywords:
  - project record
  - runtime profile
  - repository refs
see_also:
  - ./README.md
  - ./concepts.md
---

# Projects, Runtimes And Refs

Problem Board serves three kinds of project (operator, 2026-09-22):

1. **KDCube maintainer:** KDCube itself changes, and the team refreshes and
   reloads a KDCube deployment.
2. **App builder:** agents build an app on a KDCube deployment, and configure,
   reconfigure and reload that app.
3. **Independent project:** nothing to do with KDCube, and not running on it.

The worker procedure is the same for all three. What differs is data the
project declares: its instructions, its repositories and its runtimes. This
page defines the four concepts that data is made of, shows each case as a
worked example, and names where a project's setup lives.

## Four concepts

1. **Runtime (service locality).** A named place where the project's system
   runs and the team acts on it: a KDCube deployment, a staging server, a
   device, or none. For each runtime the project declares its actions
   (refresh, reload, restart, deploy, test), who may trigger each one, and the
   host it is triggered from. A runtime names a **profile**: the procedure
   document with the commands for that kind of runtime. The generic worker
   procedure carries no runtime's commands.
2. **The integration point is a git ref.** Work from many agents on many
   machines converges on a ref (a branch or a tag) pushed where the
   coordinator and every runtime's machine can fetch it. Keeping that ref
   coherent is the coordinator's job. The rule is the same on one machine and
   on many: a checkout is not the ref until it is pushed and fetched.
3. **A runtime action releases a named ref.** "Refresh from tag X", "reload
   bundle Y at the commit `origin/main` names". The ref is fetched onto the
   runtime's machine first, and the action loads the commit it names, never
   whatever a working tree holds at that moment. The action's result names the
   ref and the commit that loaded; a result that names another commit is a
   failed action.
4. **Sources are addressed by repository alias plus commit, never by path.**
   `repo:<alias>/<path>` at a commit means the same thing on every host.
   Agents explore the project through their own checkout of a ref and through
   what the board carries (mail, attachments, journals, plan items). Nothing
   crosses machines as if on a shared filesystem unless the project publishes
   it that way.

## Worked examples

**KDCube maintainer (this project, `quickstart-works`).** Runtime `dev-main`,
kind `kdcube`, host `dev-main`, profile: the KDCube maintainer profile.
Actions `refresh` (a platform rebuild with package selectors) and
`bundle-reload`, each triggered by the coordinator from `origin/main` of its
repository. A change is live when the coordinator has integrated it onto
`main`, pushed it, fetched it on `dev-main`, and the action's receipt names
that commit. Workers on `spark1` never act on `dev-main`: they push, and ask.

**App builder.** Runtime `staging`, kind `kdcube`, the host that runs the
app's KDCube deployment, profile: the KDCube app-builder profile, which knows
bundle reload and descriptor apply but not a platform rebuild. Action
`bundle-reload` from the app repository's `release` branch, triggered by the
coordinator or the app's owner. The platform itself is someone else's.

**Independent project.** A library with no deployment declares no runtime:
its workers receive no refresh, reload or bundle instruction, and "live" means
merged and released through the project's own pipeline. A project with a
device or a server declares that runtime with its own kind and profile, for
example `deploy` from a tag, triggered by the operator from a build host.

## Where a project's setup lives

The target home is the project's **Control Card**, which already carries the
project's version-control model and access rule. Until setup is configured
there, a person sets it up by hand: the instructions file and the runtimes go
in `project-setup.json` at the root of the project's journal home, reviewed
like any journal change, and `pb worker context` returns them. The Card will
hold the same fields, so only where they are read from changes.

| Setting | What it gives | Today |
| --- | --- | --- |
| **Project instructions** (one file per project) | Every participant reads the same project rules on attending (`project_instructions_ref`), whatever repositories it touches. Repository `AGENTS.md` files keep describing how to work in their code. | `instructions_ref` in `project-setup.json` |
| **Repositories of the project** | Aliases, so `repo:<alias>/...` resolves to each host's checkout; assignments carry and validate repository plus base commit; each repository's integration ref, from which runtime actions release; a per-host check that the repositories and Git access exist; which repositories are in scope. | assignment bindings and each host's repository map; each runtime action's `releases`: a repository alias and its integration ref |
| **Additional skills** | Project-specific know-how installed for every participant, such as a runtime profile. | each runtime's `profile_ref`, read from `pb worker context` |
| **Runtimes** | Where the project's systems run, their actions, who may trigger them, from which ref. | `runtimes` in `project-setup.json` |
| **Journal home** | Where the project's decisions and history are kept, and where this setup file sits. | a project setting already (`pb worker context`) |
| **Participants' default Card** | The operations a new worker starts with. | the profile the operator authorizes each worker with |

### `project-setup.json`

```json
{
  "schema": "problem-board.project-setup.v1",
  "instructions_ref": "repo:<alias>/<path>/project-instructions.md",
  "runtimes": [
    {
      "name": "dev-main",
      "host": "dev-main",
      "kind": "kdcube",
      "profile_ref": "repo:app-ecosystem/products/kdcube/procedures/runtime-profile-maintainer.md",
      "actions": {
        "refresh": {"who": ["coordinator"], "releases": [
          {"repository": "kdcube", "ref": "origin/main"},
          {"repository": "app-ecosystem", "ref": "origin/main"}
        ]},
        "bundle-reload": {"who": ["coordinator"], "releases": [
          {"repository": "applications", "ref": "origin/main"}
        ]}
      }
    }
  ]
}
```

Every action must name `who` and `releases`: each repository it loads, by alias, and the git ref it releases there, so its result can name the commit that loaded in each. An entry that cannot be read is
left out and named in `project_setup_issues`; a missing file gives empty
fields. Neither ever fails `pb worker context`.

### Where a host reads the project's setup

A host's repository map (`journal_workspace.source_repositories` in the host
relay config) maps each alias to its checkout. That checkout is where writes
go, and on a host it is often an operator's working checkout, which cannot
fast-forward over their uncommitted edits. A setup read there goes stale, and
a stale setup looks like no setup. So an entry may also name a **read root**:

```json
"source_repositories": {
  "applications": {
    "root": "/home/me/src/applications",
    "read_root": "/home/me/src/.read/applications",
    "read_ref": "origin/main"
  },
  "kdcube": "/home/me/src/kdcube"
}
```

A plain string is a root only. `read_ref` defaults to `origin/main`; the read
root must be inside an approved coding root like the root.

- **Create it once**, a dedicated detached worktree per alias, never edited:
  `git -C <checkout> worktree add --detach --relative-paths <read-root> origin/main`
  (before git 2.48, without `--relative-paths`, then rewrite the gitdir links
  to relative paths as the KDCube runtime profile shows).
- **Reads go through it.** `pb worker context` reads `project-setup.json`,
  `project-facts.md`, `project-environment.md`, the instructions file and each
  runtime's profile through the alias's read root, and returns
  `journal_home_commit` (the commit they were read at) and
  `journal_home_read_root`. Without a read root, both come from the root.
- **A lag is named, never fatal.** When the read root is behind its
  `read_ref`, missing, or not a git tree, `project_setup_issues` names the
  alias, the read root, both commits and the reason. The context compares
  local refs only; it never fetches.
- **The relay advances it, only when clean.** Its housekeeping, at most every
  five minutes per alias, fetches the ref's branch and, when
  `git status --porcelain` is empty, checks the read root out detached at the
  ref. A dirty tree or a failed fetch is left as it is and logged once per
  change.
- **Writes never go there.** Journal entries and every other change are
  committed on the agent's own branch in its own worktree and land through a
  pull request; the journal workspace's links and index use the root.
