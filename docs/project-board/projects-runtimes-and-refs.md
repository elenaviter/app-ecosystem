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
| **Repositories of the project** | Aliases, so `repo:<alias>/...` resolves to each host's checkout; assignments carry and validate repository plus base commit; each repository's integration ref, from which runtime actions release; a per-host check that the repositories and Git access exist; which repositories are in scope. | assignment bindings and each host's repository map; the integration ref in each runtime action's `from_ref` |
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
        "refresh": {"who": ["coordinator"], "from_ref": "origin/main"},
        "bundle-reload": {"who": ["coordinator"], "from_ref": "origin/main"}
      }
    }
  ]
}
```

Every action must name `from_ref` and `who`. An entry that cannot be read is
left out and named in `project_setup_issues`; a missing file gives empty
fields. Neither ever fails `pb worker context`.
