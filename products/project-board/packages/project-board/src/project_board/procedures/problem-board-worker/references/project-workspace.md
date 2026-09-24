---
id: applications.playground.problem-board.skill-reference.project-workspace
title: Set Up A Project Workspace
summary: How a worker sets up its workspace for a project it attends, from the project's record on its host, the journal repository included, each repository at its declared branch in a folder named by its alias.
tags: [procedure, problem-board, worker, workspace, repositories, attendance]
keywords: [pb worker context, project_on_this_host, repositories, alias, branch, path, role journal, clone, fetch, fast-forward, deploy key, project card, attendance]
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
  where `<workspace>` is the `workspace` field of the same output: the folder
  this session enrolled from, which the host procedure makes your own. When it
  is empty, the output says so: run `pb worker listen` from your workspace
  folder once, and read again. Two entries with the same URL are two folders,
  one per alias.
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
  named=$(ssh ${SSH_CONFIG:+-F "$SSH_CONFIG"} -G "$1" 2>/dev/null | awk '$1 == "hostname" {print $2; exit}')
  echo "${named:-$1}"
}
repository() {
  local url="${1%.git}" host path
  case "$url" in
    *://*) host="${url#*://}"; host="${host#*@}"; path="${host#*/}"; host="${host%%/*}" ;;
    *:*) host="${url%%:*}"; host="${host#*@}"; path="${url#*:}" ;;
    *) echo "$url"; return ;;
  esac
  echo "$(resolve_host "$host")/$path"
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
