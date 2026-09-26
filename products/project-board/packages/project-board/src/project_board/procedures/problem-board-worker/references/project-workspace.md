---
id: project-board.skill-reference.project-workspace
title: Set Up A Project Workspace
summary: How a worker sets up its workspace for a project it attends, from the project's record on its host, the journal repository included, each repository at its declared branch in a folder named by its alias, and why every project page and journal entry is read from that clone and nowhere else.
tags: [procedure, problem-board, worker, workspace, repositories, attendance]
keywords: [pb worker context, project_on_this_host, journal_clone, journal_home_commit, own clone, repositories, alias, branch, path, role journal, clone, fetch, fast-forward, deploy key, project card, attendance]
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

The journal repository (role `journal`) is cloned like any other: the
project's history lives there, and you read it before acting on a subject.

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
the reason otherwise. The relay carries it on its next heartbeat. Run it again
after any clone, re-clone or new key, and whenever the repository list changes
(add `--project-ref` to name the project explicitly).

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

## 5. Project state comes from your clone, and nothing else

`pb worker context` reads the project's journal home and every page in it
(setup, facts, environment, instructions, runtime profiles) from your own
clone, `<workspace>/<alias>/<path>`, for every worker, coordinator included.
Your journal entries are written there, `pb worker journal-index` indexes that
clone into an index of your own, `pb worker journal-search` searches it, and a
journal view you serve to the board is read from it. No host-wide checkout is
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
