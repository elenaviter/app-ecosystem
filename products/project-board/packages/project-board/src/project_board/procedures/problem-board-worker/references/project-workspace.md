---
id: project-board.skill-reference.project-workspace
title: Set Up A Project Workspace
summary: How a worker sets up its workspace for a project it attends, from the project's record on its host, the journal repository included, each repository at its declared branch in a folder named by its alias, and why every project page and journal entry is read from that clone and nowhere else.
tags: [procedure, problem-board, worker, workspace, repositories, attendance]
keywords: [project files, project_files, project_files_editable, project.files.edit, project.files.changed, project_goal, project_card, journal_state none, pb worker connect-project, needs_key, pull_requests, github_key, pb worker git-credential, pb worker gh, pb worker context, project_on_this_host, commit_identity, user.email, --set-identity, journal_clone, journal_home_commit, own clone, repositories, alias, branch, path, role journal, clone, fetch, fast-forward, deploy key, project card, attendance]
see_also:
  - ./identity-and-authorization.md
  - ./collaboration.md
---

# Set Up A Project Workspace

When the operator adds you to a project, your host receives the project's
record: its team and the repositories set on the project card. You set up your
workspace from that record, when you are added and each time you resume.

## 1. Read the record, once it is on this host

```bash
pb worker context --project-ref <project> --format brief
```

Read `project_on_this_host` first.

- **`false`**: the record has not reached this host yet. The relay writes it
  within seconds of the link. `repositories` is empty only because nothing is
  here yet. Wait a minute and read again. After ten minutes still `false`,
  tell the operator that the relay has not written the project on this host,
  with the output of `pb worker inspect`.
- **`true`**: the record is here, and `repositories` is the project card's
  list. An empty list then means the card names no repositories yet: ask the
  coordinator which repositories the work needs, rather than guessing.

Each entry has an `alias`, a `url`, a `role` (`work`, `journal` or
`artifact`), and an optional `branch` and `path`.

- **`alias`** names the folder: the repository lives at `<workspace>/<alias>`,
  where `<workspace>` is the `workspace` field of the same output: your own
  folder, the one this session enrolled from when it lies inside the host's
  agent workspace root, else `<agent workspace root>/<your alias>`
  (`workspace_source: host_root`). The root is the host's setting (`pb host
  configure --agent-workspace-root`, inside an approved work root), else
  the alphabetically first approved work root. Create it if it does not exist yet and work from it. Never the
  folder a session happened to start in, and never a path you choose: on
  2026-09-26 an agent started in a shared checkout was handed that checkout.
  When the output names no workspace, the host approves no root: ask the
  operator to add one. Two entries with the same URL are two folders, one per
  alias.
- **`branch`** is the branch you work on. Without it, you use the branch the
  remote checks out by default.
- **`path`** is the part of the repository the project uses, for example the
  journal home inside the journal repository. It is a place inside the clone,
  never a separate clone.

## 2. Clone or update every repository, the journal one included

`pb worker connect-project --format brief` does this step and step 3 in one
command, for every repository of the record (W304 finding 19). It clones or fast-forwards each repository as the rules below say. For a
GitHub repository, it first asks for your owner's GitHub key (W371): when
your owner has connected GitHub on the project's My Card and the GitHub App
covers the repository, a new clone is made over HTTPS, an existing one's origin
moves to HTTPS (`origin_switched: ssh_to_https`; fetch and push), and every clone gets
`pb worker git-credential` as its credential helper for github.com, with your
owner's commit email. Push with `pb worker push -- <git push arguments>`: it is
`git push`, and when your owner's key is unavailable (Connection Hub cannot
answer) it pushes the same branch through this machine's deploy key, the
`deploykey` remote connect-project kept, and says so; after a refusal it never
does. No token is stored: git asks the helper each time. The
row's `github_key` reads `ready` or the reason it is not (`github_not_linked`,
`commit_email_not_set`, `card_denies`, `not_attending`,
`repository_not_on_card`: tell your owner its words; [GitHub access for
agents](repo:app-ecosystem/products/project-board/docs/github.md)). `pb worker
context` names the email you commit with and its `source`: your owner's My Card
email (`owner_github_key`) once the key has answered, else the project's. When the key is
refused, and for a GitHub repository this machine does not reach yet, it
makes this machine's deploy key and prints the grant for the person
(add-a-worker-host step 7). It
then sets the commit identity and runs the workspace report. Run it again
after the person adds a key. first-run's "Part 2: Connect To A Project" says
what to tell the person for each state. The script below is what the command
does for one repository, and it is what to run by hand when the command is
not available:

For each entry, with `WORKSPACE` from the output's `workspace`, and `ALIAS`,
`URL` and `BRANCH` (empty when the entry has none) from the entry.
`SSH_CONFIG` stays empty, so `ssh` reads your own SSH configuration:

```bash
resolve_host() {
  local named
  if [ -n "${SSH_CONFIG:-}" ]; then
    named=$(ssh -F "$SSH_CONFIG" -G "$1" 2>/dev/null | awk '$1 == "hostname" {print $2; exit}')
  else
    named=$(ssh -G "$1" 2>/dev/null | awk '$1 == "hostname" {print $2; exit}')
  fi
  echo "${named:-$1}"
}
repository() {
  local repo_url="${1%.git}" repo_host repo_path
  case "$repo_url" in
    *://*) repo_host="${repo_url#*://}"; repo_host="${repo_host#*@}"; repo_path="${repo_host#*/}"; repo_host="${repo_host%%/*}" ;;
    *:*) repo_host="${repo_url%%:*}"; repo_host="${repo_host#*@}"; repo_path="${repo_url#*:}" ;;
    *) echo "$repo_url"; return ;;
  esac
  echo "$(resolve_host "$repo_host")/$repo_path"
}
dest="$WORKSPACE/$ALIAS"
declared=$(repository "$URL")
if [ -d "$dest/.git" ]; then
  origin=$(git -C "$dest" config --get remote.origin.url)
  if [ "$(repository "$origin")" != "$declared" ]; then
    echo "$ALIAS at $dest points at $origin, the project declares $URL" >&2
    exit 3
  fi
  if [ -n "$(git -C "$dest" status --porcelain)" ]; then
    echo "$ALIAS at $dest has uncommitted changes on $(git -C "$dest" branch --show-current)" >&2
    exit 4
  fi
  git -C "$dest" fetch --prune origin
else
  clone_url="$URL"
  if [ "$(resolve_host "github-$ALIAS")" = "${declared%%/*}" ]; then
    clone_url="github-$ALIAS:${declared#*/}.git"
  fi
  git clone --quiet "$clone_url" "$dest"
fi
if [ -z "$BRANCH" ]; then
  git -C "$dest" remote set-head origin --auto >/dev/null
  BRANCH=$(git -C "$dest" symbolic-ref --short refs/remotes/origin/HEAD)
  BRANCH=${BRANCH#origin/}
fi
git -C "$dest" checkout --quiet "$BRANCH"
git -C "$dest" merge --ff-only --quiet "origin/$BRANCH"
```

Without a declared branch, the remote's default branch is the one you work
on, read from the remote each time, whatever branch the folder was left on.

Two remotes name the same repository when they reach the same host and path.
A host with deploy keys (add-a-worker-host step 7) gives each repository its
own SSH alias, `github-<alias>`, so a clone there has an origin such as
`github-applications:kdcube/applications.git`. That matches the declared
`git@github.com:kdcube/applications.git`, because the alias resolves to
github.com in the SSH configuration. A new clone uses that alias whenever it
exists, since the deploy key is what grants access on that host.

The journal repository (role `journal`) is optional and cloned like any other.
The project may assign that role to any repository and path; no common
procedure assumes a repository name. When present, the project's history lives
there, and you read it before acting on a subject. When absent, journal work
does not apply and decisions and findings stay on the plan item.

A folder whose `origin` is not the declared URL stops there (exit 3): the
project card changed the alias, or the folder holds another repository. Do not
repoint or replace it. Tell the operator the alias, both URLs and the folder,
and go on with the rest.

A folder with uncommitted changes, new files not yet added included, stops
there (exit 4), on the branch it is on, because a checkout would carry that
work to another branch. Commit or put
the work aside yourself first, or tell the coordinator the alias if it is not
yours.

A checkout or fast-forward that fails (a history that has diverged) is never
forced. Leave that folder as it is and tell the coordinator
which alias and why.

**Commit as the project says.** When `pb worker context` returns a
`commit_identity`, every clone commits as your alias and the project's email,
set repository-local, so your worktrees inherit it. Set it in every clone you
already have, now, not only after a fresh clone: run each of its `commands`,
or let step 3 set it with `pb worker workspace-report --set-identity`. Never
commit with an email you made up: a commit is credited to whichever account
owns its email (operator, 2026-09-27). An empty `commit_identity` means the
project sets none; ask the coordinator before your first commit.

## 3. Report what you cannot reach

A repository you cannot clone or fetch (no deploy key on this host, no
access): tell the operator by name, with its alias and URL, and go on with the
rest. The operator adds the key or the access, and you clone it then.

Then tell the board what your workspace holds, so the project card shows it
next to your name:

```bash
pb worker workspace-report
```

It checks `<workspace>/<alias>` for every listed repository: `verified` when
it is a checkout of the listed URL and the remote answers, `unreachable` with
the reason otherwise. When the project sets a commit email, each clone also
reads `matches`, `differs` or `unset` for its commit identity, and the project
card shows a clone that is not `matches`; `--set-identity` sets it in each
clone first. The relay carries it on its next heartbeat. Run it again
after any clone, re-clone or new key, and whenever the repository list or the
commit email changes (add `--project-ref` to name the project explicitly).

## 4. Set up the project's development environment

After every reachable repository is present, read the environment page that
the same `pb worker context` result names as `project_environment_ref`. Its
`local_project_environment` is the resolved file in your clone of the journal
repository (step 5). The page owns this project's interpreters, virtual
environments, source overlays, test commands, fixtures, and machine-owned
system packages.

Onboarding does not build it: build and prove the environment from that page
on demand, before the first test or build you run, and before interpreting a
test failure. When the context has no environment-page ref, or a command needs an
undeclared dependency, tell the coordinator exactly what is missing. The team
adds the setup or correction to the project page, so the next worker starts
from the prepared answer.

## Project files: read them first, reread them when they change

Project files are the project's shared, current knowledge. Every agent of the
project is told where they are, reads them in its own clone, and follows them
(operator, 2026-09-27). They live in the project's repositories; the project
card lists each as a repository alias and a path, and the board keeps no copy.

When an agent starts or joins the project, `pb worker context` gives it the
three purpose files, each with its path in this agent's clone and whether it
is there, and the list of further files with their one-line descriptions:

- `project_instructions_ref`, `local_project_instructions`,
  `project_instructions_state`: **Instructions**, what the project is, its
  rules and conventions, how work is done there.
- `project_facts_ref`, `local_project_facts`, `project_facts_state`:
  **Facts**, the decisions and rulings in force now.
- `project_environment_ref`, `local_project_environment`,
  `project_environment_state`: **Environment**, machines, runtimes, how to
  test and deploy.
- `project_files`: the further files, each with `ref`, `description`,
  `local_path` and `state`.

A state is `present`, `missing` (your clone lacks the file: fetch as step 2
says, then tell the coordinator if it is still missing) or `not_cloned` (set
the repository up with `pb worker connect-project`). An empty purpose ref
means the card names no such file; the journal home never stands in for it.

**Read them first, before any work.** Read a further file when its
description fits the task at hand. The descriptions are a table of contents,
not a reading list.

**While you work:** when a file changes, or one is added to the list, agents
are told on their next check and reread it. `pb worker receive` names each
changed file (`SIGNAL project.files.changed`, with its ref and whether it was
added, changed or removed) until you run `pb worker context` again; reread
them before you go on. One edit changes what every agent on every machine
does. A content change counts once your clone has it, so fetch as step 2 says
when you resume.

**Editing them.** When the person makes a ruling, the coordinator writes it
into Facts: rulings live in project files, not in any agent's private memory.
With `project_files_editable: true` (your Card holds `project.files.edit`),
edit a project file like any file in its repository: a branch, a commit and a
pull request, or a direct commit where the project allows it. Without it,
propose the change to the coordinator by mail: the file, the change and why.

A person may also edit a project file on the card. The board never writes
it: the coordinator's relay applies the edit in the coordinator's clone, in a
worktree from the branch, and refuses it when the file changed since the
person opened it. It commits as the coordinator ("Project file <path>: edited
by <person> on the board") and pushes by the repository's `file_edits`
setting on the card: a direct commit for a local repository, a pull request
for a remote one. Every agent is then told on its next check, as for any
change.

**How this differs from a journal:** project files are the current truth
(what applies now), and every project has them. A journal is history (what
happened and why), and it stays optional. With no journal
(`journal_state: none`, not an error) the project files are the record:
decisions and findings go in the plan item's notes (`plan.note.append`), and
no journal search or journal write applies. `journal_state: unavailable`
still means a journal the project declares that this agent cannot read yet
(step 5).

**A board that predates project files** sends no list: `project_files` is
absent, and the purpose refs come from the journal home as before
(`project_card: unknown`, or `known` with the goal and facts of 2026.09.29.1142).

## 5. Project state comes from your clone, and nothing else

`pb worker context` reads the project's journal home and every page in it
(setup, facts, environment, instructions, runtime profiles) from your own
clone, `<workspace>/<alias>/<path>`, for every worker, coordinator included.
Merged journal entries are indexed there by `pb worker journal-index`,
`pb worker journal-search` searches them, and a journal view you serve to the
board is read from there. Entries are authored on an item's branch in its
worktree, never in this clean clone. After the journal change request is
merged, fetch and fast-forward the clean clone before indexing; an unmerged
entry correctly returns `journal_entry_not_found`. No host-wide checkout is
ever a source, however current it looks: another worker's or a person's
checkout lags, or carries edits you cannot see, and a page read there looks
like no page.

- `journal_home_commit` is the commit of your clone that the pages were read
  at, and `journal_clone` says how current that clone is against
  `origin/<branch>` (the declared branch, else the remote's default) as last
  fetched: `current`, `ahead`, `behind`, `diverged` or `no_upstream`. Nothing
  fetches for you.
- **Behind:** `project_setup_issues` carries the `journal_clone.action` line,
  the fetch and fast-forward of step 2. Run it and read the context again.
- **Missing:** `journal_state` is `unavailable` with
  `journal_repository_root_missing`, naming the alias and the folder. Clone it
  as step 2 says, run `pb worker workspace-report`, and read again. Nothing is
  read in its place.
- **Diverged:** never force it; tell the coordinator the alias and both
  commits, as for any fast-forward that fails.

## 6. Work in worktrees inside your workspace, and remove them when done

Every agent lays out its workspace the same way. The person and the
coordinator can then see what each agent has in hand, reviews and fixes never
disturb the work in hand, and a disk does not fill with forgotten copies
(operator and the team's Rule 7 round, 2026-09-27):

| Folder | What it is |
| --- | --- |
| `<workspace>/<alias>` | **the clean clone** from step 2. It stays on its declared branch, is clean, and is fetched and fast-forwarded (`git merge --ff-only origin/<branch>`), not only fetched. Step 5, the relay's journal views and every test overlay read its working tree. A work branch, a work in progress or a tool's commit here would change what they read. If the fast-forward refuses, the clone is not clean: stop and report it, never reset it. |
| `<workspace>/wt/<item>-<alias>` | one worktree per assignment and repository, on the assignment's work branch: `git -C <workspace>/<alias> worktree add <workspace>/wt/<item>-<alias> -b <branch> origin/<base>`. A change pair across repositories is one worktree in each. A journal edit reuses this worktree when the item already binds the journal repository; otherwise it gets `<workspace>/wt/<item>-<journal-alias>` and a feature-bound branch and change request of its own. No per-agent journal worktree or branch spans unrelated items. |
| `<workspace>/rv/<item>-<alias>-<short sha>` | one worktree per review, returned-item check or tested merge, **detached** at the exact commit you examine: `git -C <workspace>/<alias> worktree add --detach <path> <sha>`. A review never moves anyone's branch. |
| `<workspace>/scratch/<item>/<run>` | work files that are not git trees (reports, probes, payloads, logs), one run folder per job, made by `pb worker scratch --new`. Nothing else sits loose at the workspace root. |

- **Nothing goes to a temporary or hidden folder outside the workspace.** A
  copy there is invisible to the person, and nothing cleans it up.
- **Trees are not a buffer.** Keep only the trees work needs now: your current
  item's tree in each repository it changes, an exact-head review in
  progress, and a tree a runtime or a pinned release holds. Before you create
  a tree, reuse one you have for that repository (check out the next branch
  there) or read from the clean clone, and name the new tree's purpose when
  you register it. Once a branch is pushed and its evidence published, the
  pushed commits are the copy: end a clean, fully pushed tree, paused change
  request included, and let the sweep remove it. Why: the shared host ran out
  of storage on 2026-09-30 and again on 2026-10-01, with up to ten local trees
  per repository kept just in case (operator, 2026-10-01).
- **Suites name their trees.** A suite that overlays other repositories takes
  each tree as an argument or variable, defaulting to the clean clones (right
  for main against main). A change pair points each at its partner worktree,
  for example `AE=<workspace>/wt/<item>-app-ecosystem`. The suite prints each
  tree's head, and the report quotes them.
- **Writing tools run in an item worktree, never in the clean clone.** `pb plan sync`
  and `pb plan import` resolve the journal home from `pb worker context`,
  which is the clean clone. Until they take an explicit journal path, run them
  from the item's repository worktree with that path (including its item-scoped
  journal worktree when the journal repository is otherwise not in the item).
  `scripts/release-pb` runs in its
  own `wt/release-<version>` tree from `origin/main`.

**Register every tree, and let the sweep remove what is finished (W423).**
Right after you create a tree, register it against the job it serves:

```bash
pb worker workspace --assignment-ref <assignment> --repository <repo> --path <workspace>/wt/<item>-<alias> --item <Wn>
pb worker workspace --kind review --assignment-ref <reviewed item work_ref> --repository <repo> --path <workspace>/rv/<item>-<alias>-<sha> --item <Wn>
```

A tree's job ends only when its end is recorded: its review decision through
`pb coordinate review.*`, or your own
`pb worker workspace --end --path <path> --reason "<change request closed | released>"`.
Being merged never ends a job: a merged tree may still serve a pending review
or a release.
A paused or suspended change request is no reason to keep its tree (operator,
2026-10-01). When the branch is pushed, the tree is clean, its evidence is
published and nothing runs from it, end the checkout with
`pb worker workspace --end --path <tree> --reason "suspended: PR <n> pushed at <head>"`.
The open item then no longer keeps the tree, and every other check below still
does. Ending a tree this way never ends or releases the assignment and never
closes the change request. To resume, recreate the tree from the pushed branch.
`pb worker workspace --sweep` lists every tree in your workspace, registered or
not, with its state, size and what `--apply` would do, and records that list.
`--apply` removes a tree only when its job ended **and** nothing could be lost,
and the last `--sweep` listed it in the same state:

- nothing unique in it: no uncommitted change, no untracked file, no commit
  that no remote has, and no gitignored file you did not declare regenerable.
  Ignored test results, screenshots and captures can be the only copy of a
  finding, and a folder name (`build`, `dist`, a cache) proves nothing. Declare
  what a command makes again:
  `pb worker workspace --path <tree> --generated node_modules --generated-by "npm ci"`;
- nothing still needs it: its item is Done or Cancelled on the board (an open
  item, or a state the sweep cannot read, keeps it), no consumer pinned it
  (`pb worker workspace --path <tree> --pin "<release or review>"`, cleared
  with `--unpin`), no other tree links into it, and it is not a protected path.

It uses `git worktree remove` without force, `git branch -d` for a merged
branch and `git worktree prune`, and names every tree it keeps with the reason.
The clean clone is never removed.

**Work files live in a scratch run, and go only after their content is safe
(W423).** Reports, probes, payloads and logs that are not part of a tree go
into one run folder per job:

```bash
pb worker scratch --new --item <Wn> --purpose "<why this run exists>"
pb worker scratch --record --run <run> --file <file> --published "repo:<alias>/<path>@<commit>"
pb worker scratch --record --run <run> --file <file> --generated-by "<command that makes it again>"
pb worker scratch --close --run <run> --reason "<why the job is over>" --findings "repo:<alias>/<path>@<commit>"
```

Record each file as you finish it: where its content now lives, or the command
that makes it again. `--consumer` names a review, change request or release
that still needs the run, and `--consumer-done` clears it. Close the run when
the job is over, naming where its findings are published. Publish a unique
finding first, in an applied item note or a tracked file, then record it.
The sweep removes a run only when you closed it, its item is Done or Cancelled
on the board, every file is recorded, unchanged, not a link and published or
regenerable, the published references verify (a tracked file at a commit in
your clone's default branch, or an applied note on the run's own item, read
from the board when the sweep runs; a file's reference must hold the same
content, not only the same path, so a recorded file published as a note must
be the note's exact text), no consumer is open, and the last `--sweep` listed it unchanged. A receipt of
what was removed is written first, under `.problem-board/scratch-receipts/`.
Anything unknown, unreadable, changed or offline keeps the run, and age only
flags it for review. Files loose at the workspace root are listed by the sweep
and never removed: move them into a run.

The sweep also runs without anyone remembering: at session start
(`pb worker listen`), on `pb worker idle`, and after a review decision recorded
through `pb coordinate`. Until the operator turns it on for the host, these
automatic runs only report what they would remove
(`pb host configure --workspace-sweep-auto-apply`; `--workspace-sweep-protect
<path>` adds a path it never removes).
- Never `rm -rf` a worktree folder, and never remove the clean clone: it holds
  every worktree's Git data. A tree the sweep keeps is yours to finish, push or
  remove by hand with `git worktree remove`.
- Remote branches are the merger's to delete.
- A review tree goes as soon as the verdict is recorded and its item is
  closed. Its installed packages (for example a widget's `node_modules`) go with it
  when you declared them with `--generated`, unless another tree still links
  into them.
- The first real sweep on a host with an existing pile is the operator's
  decision, and so is turning automatic removal on; until then, `--sweep`
  without `--apply` shows what would go.

Runtime-window database backups follow the same lifetime rule: they live in
the host's managed backup folder, never in a workspace or scratch folder, and
only until the next verified ALL CLEAR keeps the newest one. The coordinator's
step is in [runtime actions](runtime-actions.md#runtime-window-database-backups).

Why: worktrees that were never removed filled one host with about a hundred
stale folders, and a coordinator's hidden test trees reached 4 GB; on
2026-09-30 one agent held 55 finished trees (about 11 GB) and the host disk
filled. A rule an agent must remember did not hold, so the sweep runs on its own.
